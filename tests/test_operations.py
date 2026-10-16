"""运维边界：备份、限流响应成本、迁移失败与部署文件的机械校验。

外部工具（systemd-analyze / nginx / openssl）在缺少时自动跳过；这类校验的价值
在于**防止文档里的写法回归**（例如 systemd 单元行尾注释会被静默忽略）。
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import support
from tests.support import ARTICLE_SLUG, PROJECT_ROOT

#: 全部 systemd 单元示例：守卫覆盖每一个（新增单元自动纳入）
SERVICE_FILES = sorted(PROJECT_ROOT.glob("manual/*.service"))
NGINX_EXAMPLE = PROJECT_ROOT / "manual" / "nginx.conf.example"


def _nginx_version():
    """本机 nginx 的版本元组；拿不到返回 None。

    `http2 on;` 是 1.25.1 起才有的指令；示例给的是现代写法，因此在旧版本上要先按注释
    里的替代写法替换那一行，再把其余指令原样交给 `nginx -t` 校验。
    """
    result = subprocess.run(["nginx", "-v"], capture_output=True, text=True, check=False)
    match = re.search(r"nginx/(\d+)\.(\d+)(?:\.(\d+))?",
                      (result.stderr or "") + (result.stdout or ""))
    return tuple(int(part) for part in match.groups(default="0")) if match else None


class DatabasePathAPI(unittest.TestCase):
    """`database.db_path()` 必须**实时**反映 `configure()` 的结果。

    历史缺陷：`database/__init__.py` 用 `from .connection import DB_PATH` 再导出，
    那只是 import 时刻的**快照** —— `configure()` 之后它不更新，于是启动日志
    打印的是默认路径而不是 config.toml 里 `database` 指定的路径
    （排障时按日志去找文件会扑空）。现在只暴露函数，不暴露快照常量。
    """

    def setUp(self):
        support.ensure_application()

    def test_db_path_tracks_configure(self):
        from elenvind import database

        original = Path(database.db_path())
        alternative = Path(support.WORKSPACE.root) / "path-api.db"
        try:
            database.configure(alternative)
            self.assertEqual(Path(database.db_path()), alternative,
                             "db_path() must follow configure(), not a stale snapshot")
        finally:
            database.configure(original)
        self.assertEqual(Path(database.db_path()), original)

    def test_package_does_not_export_a_stale_path_snapshot(self):
        """`database.DB_PATH` 这类 import 期快照不得再暴露（它只会误导调用方）。"""
        from elenvind import database

        self.assertFalse(hasattr(database, "DB_PATH"))
        self.assertTrue(callable(database.db_path))

    def test_startup_log_reports_the_active_path(self):
        support.ensure_application()
        from elenvind import database

        log = Path(support.WORKSPACE.log).read_text(encoding="utf-8")
        self.assertIn(str(database.db_path()), log)


class Backup(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()
        support.Session().create_account("Backup", "backup@example.test", "correct-horse-1")
        self.directory = Path(support.WORKSPACE.root) / "backups"
        self.directory.mkdir(exist_ok=True)

    def test_backup_is_consistent_and_private(self):
        from elenvind import database

        target = self.directory / "unit.db"
        written = database.backup_to(target)
        self.assertEqual(written, target)
        # 备份含用户 / 会话 / 登录流水：必须不受调用方 umask 影响
        self.assertEqual(oct(target.stat().st_mode & 0o777), oct(0o600))
        conn = sqlite3.connect(target)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             database.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_refuses_to_overwrite_an_existing_backup(self):
        from elenvind import database

        target = self.directory / "again.db"
        database.backup_to(target)
        with self.assertRaises(database.BackupError):
            database.backup_to(target)

    def test_failed_backup_never_touches_the_existing_copy(self):
        """备份失败绝不能破坏已有的有效备份（逐字节比对）。"""
        import hashlib

        from elenvind import database

        target = self.directory / "keep.db"
        database.backup_to(target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with self.assertRaises(database.BackupError):
            database.backup_to(target)                     # 目标已存在 → 直接拒绝
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest)

    def test_restored_database_supports_reads_and_writes(self):
        """恢复后的库必须能正常读写（走应用层真实路径，而不只是 PRAGMA）。"""
        import shutil

        from elenvind import database

        support.Session().create_account("Restore", "restore@example.test", "correct-horse-1")
        backup = self.directory / "restore.db"
        database.backup_to(backup)

        restored = Path(support.WORKSPACE.root) / "restored.db"
        shutil.copyfile(backup, restored)            # 模拟"把备份放回去"
        original = database.db_path()
        try:
            database.configure(restored)
            self.assertIsNotNone(database.get_user_by_email("restore@example.test"))   # 读
            user_id = database.create_user("After", "after@example.test", "hash")      # 写
            self.assertIsNotNone(database.get_user_by_id(user_id))
        finally:
            database.configure(original)                   # 别影响其它用例

    def test_missing_database_is_reported(self):
        from elenvind import database

        with self.assertRaises(database.BackupError):
            database.backup_to(self.directory / "x.db",
                         db_path=Path(support.WORKSPACE.root) / "does-not-exist.db")


class RateLimitResponseCost(unittest.TestCase):
    """被限流的请求不该渲染整页 —— 否则"达到限制"本身就成了放大器。"""

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        config = support.live_config()
        self._saved = config["comment_limits"]
        config["comment_limits"] = {"max_per_user": 1, "max_per_ip": 100,
                                    "window_seconds": 60}
        self.user = support.Session()
        self.user.create_account("Lim", "lim@example.test", "correct-horse-1")

    def tearDown(self):
        support.live_config()["comment_limits"] = self._saved

    def test_rate_limited_response_is_lightweight(self):
        marker = "COMMENT_MARKER_XYZ"
        self.assertEqual(self.user.comment(ARTICLE_SLUG, marker).status, 302)
        limited = self.user.comment(ARTICLE_SLUG, "second")
        article = self.user.get(f"/article/{ARTICLE_SLUG}")
        self.assertEqual(limited.status, 429)
        self.assertLess(len(limited.body), len(article.body) * 0.5,
                        "the 429 must not render the whole article page")
        self.assertNotIn(marker.encode(), limited.body,
                         "the 429 must not include the comment tree")


class RegisterPreCheck(unittest.TestCase):
    """被限流的公开注册必须**不再**碰全局写锁。

    机械证明：把 `database.auth.write_tx` 换成会爆炸的替身 —— 如果预检没生效、
    超限请求仍然进入写事务，测试会以内层 `AssertionError` 失败。
    """

    def setUp(self):
        support.ensure_application()
        support.reset_database()
        from elenvind import database

        self.db = database
        config = support.live_config()
        self._saved = config["register_limits"]
        config["register_limits"] = {"max_per_ip": 2, "window_seconds": 3600}
        for _ in range(2):                     # 用真实记账函数把该 IP 填满
            database.auth.try_register_attempt("127.0.0.1", max_per_ip=2, window_seconds=3600)

    def tearDown(self):
        support.live_config()["register_limits"] = self._saved

    def test_saturated_ip_is_rejected_without_touching_the_write_lock(self):
        from unittest import mock

        with mock.patch("elenvind.database.auth.write_tx",
                        side_effect=AssertionError("pre-check must skip the write lock")):
            result = support.Session().register("Over", "over@example.test", "correct-horse-1")
        self.assertEqual(result.status, 200)
        self.assertIn("too many", result.text().lower())

    def test_ip_below_the_limit_can_still_register(self):
        support.reset_database()               # 清空窗口：预检不得误杀正常注册
        self.assertEqual(
            support.Session().register("Ok", "ok@example.test", "correct-horse-1").status, 302)


class MigrationSafety(unittest.TestCase):
    def setUp(self):
        support.ensure_application()
        support.reset_database()

    def test_init_db_is_idempotent(self):
        from elenvind import database

        database.init_db()
        database.init_db()
        self.assertEqual(support.query("PRAGMA user_version")[0][0], database.SCHEMA_VERSION)

    def test_leftover_legacy_table_blocks_startup(self):
        from elenvind import database
        from elenvind.database.migration import MigrationError

        with database.write_tx() as conn:
            conn.execute("CREATE TABLE comment_legacy (id INTEGER)")
        try:
            with self.assertRaises(MigrationError):
                database.init_db()
        finally:
            with database.write_tx() as conn:        # 收拾干净，别影响其它用例
                conn.execute("DROP TABLE comment_legacy")

    def test_refuses_a_database_from_a_newer_schema(self):
        """旧代码 + 新库必须拒绝启动（否则会以每请求报错的形式失败）。

        判定必须是"只升不降"：`version >= SCHEMA_VERSION` 时直接返回
        它一声不吭地继续跑，把"结构不认识"留给每个请求去撞。
        """
        from elenvind import database
        from elenvind.database.migration import MigrationError

        with database.write_tx() as conn:
            conn.execute(f"PRAGMA user_version={database.SCHEMA_VERSION + 1}")
        try:
            with self.assertRaises(MigrationError):
                database.init_db()
        finally:
            with database.write_tx() as conn:
                conn.execute(f"PRAGMA user_version={database.SCHEMA_VERSION}")

    def test_v4_email_collision_aborts_without_writing(self):
        """v4 小写化会撞 UNIQUE 时必须**先中止**，而不是让整站起不来。

        迁移必须**先检查**再改写：直接 `UPDATE ... lower(email)` 时，库里同时存在
        `Dup@Example.com` 与 `dup@example.com`（BINARY UNIQUE 允许并存）时，
        IntegrityError 会让每个 worker 的导入期都失败，只能手工 SQL 救。
        """
        from elenvind import database
        from elenvind.database.migration import MigrationError

        with database.write_tx() as conn:
            for email in ("Dup@Example.com", "dup@example.com"):
                conn.execute(
                    "INSERT INTO user (nickname, email, password, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (email, email, "x", "2026-01-01T00:00:00"))
            conn.execute("PRAGMA user_version=3")
        try:
            with self.assertRaises(MigrationError):
                database.init_db()
            rows = support.query("SELECT email FROM user ORDER BY email")
            self.assertEqual([row[0] for row in rows],
                             ["Dup@Example.com", "dup@example.com"],
                             "中止必须发生在写入之前：大小写不得被改动")
            self.assertEqual(support.query("PRAGMA user_version")[0][0], 3)
        finally:
            with database.write_tx() as conn:
                conn.execute("DELETE FROM user")
                conn.execute(f"PRAGMA user_version={database.SCHEMA_VERSION}")

    def test_destructive_migration_snapshots_the_database_first(self):
        """v6 就地涂黑评论前必须留一份可恢复快照。

        v6 会把"软删除但正文仍在"的评论永久替换成黑块；一次普通重启不应该
        在没有任何退路的情况下销毁数据。
        """
        from elenvind import database

        with database.write_tx() as conn:
            conn.execute(
                "INSERT INTO user (id, nickname, email, password, created_at) "
                "VALUES (1, 'a', 'a@example.test', 'x', '2026-01-01T00:00:00')")
            conn.execute(
                "INSERT INTO comment (article_slug, user_id, content, created_at, "
                "is_deleted) VALUES ('post', 1, 'the original words', "
                "'2026-01-01T00:00:00', 1)")
            conn.execute("PRAGMA user_version=5")

        workspace = Path(support.WORKSPACE.db).parent
        before = set(workspace.glob("*.pre-v6-*.db"))
        try:
            database.init_db()
            created = set(workspace.glob("*.pre-v6-*.db")) - before
            self.assertEqual(len(created), 1, "破坏性迁移前必须写一份快照")

            conn = sqlite3.connect(created.pop())
            try:
                content = conn.execute("SELECT content FROM comment").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(content, "the original words", "快照必须是迁移前的数据")

            redacted = support.query("SELECT content FROM comment")[0][0]
            self.assertNotEqual(redacted, "the original words")
            self.assertTrue(set(redacted) <= {"\u2588"}, "迁移后必须只剩黑块")
        finally:
            with database.write_tx() as conn:
                conn.execute("DELETE FROM comment")
                conn.execute("DELETE FROM user")
                conn.execute(f"PRAGMA user_version={database.SCHEMA_VERSION}")
            for leftover in workspace.glob("*.pre-v6-*.db"):
                leftover.unlink()


class LogRotation(unittest.TestCase):
    """日志被外部工具（logrotate）改名后，应用必须能继续写。

    `[logging].rotate = false` 时用的是 `WatchedFileHandler`：它在每次写之前
    检查 dev/ino，发现文件被换掉就重新打开 —— 这正是多 worker 部署下唯一安全
    的做法。这里用真实的 handler 走一遍：写 → 改名 → 再写。
    """

    def setUp(self):
        support.ensure_application()

    def tearDown(self):
        # 恢复测试夹具原本的日志目标，别影响其它用例对日志内容的断言
        from elenvind.core.logging_config import setup_logging

        config = support.live_config()["logging"]
        setup_logging(level=config["level"], log_file=config["file"],
                      max_bytes=config["max_bytes"], backup_count=config["backup_count"],
                      rotate=config.get("rotate", False))

    def test_handler_reopens_the_log_after_external_rotation(self):
        import logging

        from elenvind.core.logging_config import setup_logging

        path = Path(support.WORKSPACE.root) / "rotation.log"
        setup_logging(level="info", log_file=str(path), rotate=False)
        logger = logging.getLogger("elenvind.tests.rotation")
        try:
            logger.info("before-rotation")
            self._flush()
            self.assertIn("before-rotation", path.read_text(encoding="utf-8"))

            path.rename(path.with_suffix(".log.1"))          # 模拟 logrotate
            logger.info("after-rotation")
            self._flush()

            self.assertTrue(path.exists(), "the log file must be recreated")
            self.assertIn("after-rotation", path.read_text(encoding="utf-8"))
        finally:
            for handler in logging.getLogger().handlers:
                handler.flush()

    @staticmethod
    def _flush() -> None:
        import logging

        for handler in logging.getLogger().handlers:
            handler.flush()


class DeploymentFiles(unittest.TestCase):
    """部署文件的机械校验（能跑外部工具就跑，跑不了退化为静态断言）。"""

    def _nginx_directive(self, name: str) -> str:
        text = NGINX_EXAMPLE.read_text(encoding="utf-8")
        line = next((item for item in text.splitlines() if item.strip().startswith(name)),
                    None)
        self.assertIsNotNone(line, f"{name} is missing from manual/nginx.conf.example")
        return line.split(None, 1)[1].strip().rstrip(";")

    @staticmethod
    def _to_bytes(value: str) -> int:
        suffixes = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
        text = value.strip().lower()
        if text[-1] in suffixes:
            return int(float(text[:-1]) * suffixes[text[-1]])
        return int(text)

    def test_nginx_body_limit_leaves_room_for_the_application_limit(self):
        """请求体上限必须"应用先触发、Nginx 兜底"。

        若 Nginx 的上限 ≤ 应用上限，Nginx 会抢先返回 413，应用自己的 413
        （有日志、有测试）在反代后面永远观察不到 —— 这条守的就是那个顺序。
        """
        import tomllib

        with open(PROJECT_ROOT / "elenvind" / "resources" / "config.example.toml",
                  "rb") as handle:
            example = tomllib.load(handle)
        application_limit = int(example["max_body_size"])
        nginx_limit = self._to_bytes(self._nginx_directive("client_max_body_size"))
        self.assertGreater(nginx_limit, application_limit,
                           f"nginx {nginx_limit} must exceed the application limit "
                           f"{application_limit}")

    def test_nginx_proxy_read_timeout_exceeds_gunicorn_timeout(self):
        """代理读超时必须大于 Gunicorn 的 worker 超时（默认 30s），否则两边同时超时。"""
        timeout = int(self._nginx_directive("proxy_read_timeout").rstrip("s"))
        self.assertGreater(timeout, 30, "proxy_read_timeout must exceed gunicorn's 30s")

    def test_systemd_units_exist(self):
        self.assertTrue(SERVICE_FILES, "manual/ 下应当有 systemd 单元示例")

    def test_systemd_unit_has_no_trailing_comments(self):
        """systemd **不**把行尾 `#` 当注释：整行会被并入参数值并被静默忽略。"""
        offenders = []
        for path in SERVICE_FILES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "#" in stripped:
                    offenders.append(f"{path.name}:{number}: {stripped}")
        self.assertEqual(offenders, [], f"trailing comments in a systemd unit: {offenders}")

    def test_read_write_paths_are_narrow(self):
        """服务账号不得拥有整个项目树的写权限（代码/模板/配置必须只读）。"""
        for path in SERVICE_FILES:
            line = next((text for text in path.read_text(encoding="utf-8").splitlines()
                         if text.startswith("ReadWritePaths=")), None)
            self.assertIsNotNone(line, f"{path.name} 缺少 ReadWritePaths")
            paths = [item.lstrip("-") for item in line.split("=", 1)[1].split()]
            self.assertNotIn("/srv/elenvind", paths,
                             "the whole project tree must not be writable by the service")
            for expected in ("/srv/elenvind/data", "/srv/elenvind/logs",
                             "/srv/elenvind/backups"):
                self.assertIn(expected, paths, path.name)

    def test_units_restrict_address_families(self):
        """网络服务只需要这几个地址族：限制它属于零成本的加固。"""
        for path in SERVICE_FILES:
            self.assertIn("RestrictAddressFamilies=", path.read_text(encoding="utf-8"),
                          path.name)

    def test_shipped_default_database_lives_in_the_writable_subdirectory(self):
        """包内示例配置的默认库路径必须落在 systemd 允许写的目录里。"""
        import tomllib

        with open(PROJECT_ROOT / "elenvind" / "resources" / "config.example.toml",
                  "rb") as handle:
            example = tomllib.load(handle)
        self.assertTrue(str(example.get("database", "")).startswith("data/"),
                        f"database must default to data/: {example.get('database')!r}")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
    def test_systemd_analyze_verify_reports_no_ignored_directives(self):
        for path in SERVICE_FILES:
            result = subprocess.run(["systemd-analyze", "verify", str(path)],
                                    capture_output=True, text=True, check=False)
            output = (result.stdout or "") + (result.stderr or "")
            ignored = [line for line in output.splitlines()
                       if ("ignoring" in line or "Invalid argument" in line)
                       and "not executable" not in line]  # 本机没有 /srv/elenvind 属正常
            self.assertEqual(ignored, [], f"{path.name}: systemd ignored directives: {ignored}")

    @unittest.skipUnless(shutil.which("nginx") and shutil.which("openssl"),
                         "nginx/openssl are unavailable")
    def test_nginx_example_parses(self):
        """示例必须能被 nginx 接受（HTTP/2 一行按本机版本替换后再校验）。

        nginx 1.25.1 起才有独立的 `http2 on;`；更早的版本只能在 `listen` 上带 `http2`。
        示例给的是现代写法，因此这里先把那一行换成当前 nginx 支持的写法，其余指令
        （limit_req_zone / limit_conn_zone / proxy_* / ssl_*）全部原样交给 `nginx -t`。
        """
        example = NGINX_EXAMPLE.read_text(encoding="utf-8")
        version = _nginx_version()
        if version and version < (1, 25, 1):
            for modern in ("    listen 443 ssl;\n    listen [::]:443 ssl;\n    http2 on;",
                           "    listen 443 ssl;\n    listen [::]:443 ssl;\n"
                           "    # HTTP/2：nginx >= 1.25.1 用 `http2 on;`；更早的版本"
                           "（如 Ubuntu 24.04 自带的\n    # 1.24）不认识它，改用 "
                           "`listen 443 ssl http2;` 并删掉这一行。\n    http2 on;"):
                example = example.replace(
                    modern, "    listen 443 ssl http2;\n    listen [::]:443 ssl http2;")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            certificate = workspace / "cert.pem"
            key = workspace / "key.pem"
            generated = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(certificate), "-days", "1",
                 "-subj", "/CN=example.com"],
                capture_output=True, text=True, check=False)
            if generated.returncode != 0:                  # 证书生成失败则跳过而非误报
                self.skipTest(f"openssl could not generate a test certificate: {generated.stderr}")
            site = (example
                    .replace("/etc/letsencrypt/live/example.com/fullchain.pem", str(certificate))
                    .replace("/etc/letsencrypt/live/example.com/privkey.pem", str(key))
                    .replace("/var/log/nginx/elenvind.access.log", str(workspace / "access.log"))
                    .replace("/var/log/nginx/elenvind.error.log", str(workspace / "error.log")))
            (workspace / "site.conf").write_text(site, encoding="utf-8")
            (workspace / "nginx.conf").write_text(
                "worker_processes 1;\n"
                f"error_log {workspace / 'error.log'};\n"
                f"pid {workspace / 'nginx.pid'};\n"
                "events { worker_connections 64; }\n"
                "http {\n"
                "  access_log off;\n"
                f"  include {workspace / 'site.conf'};\n"
                "}\n", encoding="utf-8")
            result = subprocess.run(
                ["nginx", "-t", "-c", str(workspace / "nginx.conf"), "-p", str(workspace)],
                capture_output=True, text=True, check=False)
            output = (result.stdout or "") + (result.stderr or "")
            # 非特权端口 bind 失败属环境限制；这里只要求语法/上下文被接受
            self.assertIn("syntax is ok", output, output)


if __name__ == "__main__":
    unittest.main()
