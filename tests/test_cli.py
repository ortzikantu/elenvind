"""CLI 的守卫：子命令可用、危险操作必须显式许可、交互可以被关掉。

CLI 是本项目最容易"看起来能用、实际踩坑"的一块：它启动服务、还会在没人回答的时候
等输入。所以这里把每条分支都钉住：`--yes` 绝不提问、缺配置时给出可执行的下一步、
以及"命令行不提供修改系统配置的能力"这条边界。
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import support
from tests.support import PROJECT_ROOT

SUBMODULES = ("init", "serve", "backup", "doctor")


def _run(argv):
    """跑一次 CLI，返回 `(退出码, 输出)`；argparse 的退出被折算成退出码。"""
    from elenvind import cli

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            code = cli.main(argv)
        except SystemExit as request:
            code = request.code if isinstance(request.code, int) else 0
    return code, buffer.getvalue()


def _fake_server(name="gunicorn", available=True, when="测试用"):
    from elenvind import launcher

    return launcher.Server(name, name.title(), name, when, lambda: available,
                           lambda *args: 0)


class HelpAndVersion(unittest.TestCase):
    def test_help_lists_every_subcommand(self):
        code, output = _run(["--help"])
        self.assertEqual(code, 0)
        for name in SUBMODULES:
            self.assertIn(name, output)

    def test_subcommand_help_is_available(self):
        for name in SUBMODULES:
            code, output = _run([name, "--help"])
            self.assertEqual(code, 0, name)
            self.assertIn("usage", output.lower(), name)

    def test_version_flag_reports_the_packaged_version(self):
        from elenvind.core.version import raw_version

        code, output = _run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(raw_version(), output)

    def test_unknown_subcommand_is_a_usage_error(self):
        code, output = _run(["nope"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", output.lower())


class Init(unittest.TestCase):
    def test_creates_a_complete_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, output = _run(["init", "--dir", temporary, "--domain", "example.test",
                                 "--title", "My Site"])
            self.assertEqual(code, 0, output)
            root = Path(temporary)
            for name in ("config.toml", ".gitignore", "articles", "custom", "data",
                         "logs", "backups"):
                self.assertTrue((root / name).exists(), name)
            text = (root / "config.toml").read_text(encoding="utf-8")
            self.assertIn('site_url = "https://example.test"', text)
            self.assertIn('title = "My Site"', text)

    def test_generated_config_passes_the_application_validation(self):
        """init 的产物必须能直接启动：拿应用自己的校验器验一遍。"""
        import tomllib

        from elenvind.core.config import validate_config

        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(_run(["init", "--dir", temporary, "--domain",
                                   "example.test"])[0], 0)
            with open(Path(temporary) / "config.toml", "rb") as handle:
                candidate = tomllib.load(handle)
            validate_config(candidate)          # 不抛异常即通过

    def test_existing_config_is_kept_unless_forced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _run(["init", "--dir", temporary, "--domain", "first.test"])
            config = root / "config.toml"
            self.assertIn("first.test", config.read_text(encoding="utf-8"))

            code, output = _run(["init", "--dir", temporary, "--domain", "second.test"])
            self.assertEqual(code, 0)
            self.assertIn("未改动 config.toml", output)     # 明确告知跳过了什么
            self.assertIn("first.test", config.read_text(encoding="utf-8"),
                          "默认不得覆盖已有的 config.toml")

            self.assertEqual(_run(["init", "--dir", temporary, "--domain",
                                   "second.test", "--force"])[0], 0)
            self.assertIn("second.test", config.read_text(encoding="utf-8"))


class Serve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def setUp(self):
        from elenvind import cli, launcher

        self.cli, self.launcher = cli, launcher
        self.config = support.test_config(support.WORKSPACE)
        # 关掉交互，并把 launch 换成记录器：**任何**用例都不可能真的起一个服务，
        # 也不会在端口上撞到用户自己的实例。
        self.launched: list[dict] = []

        def fake_launch(server, bind, source, workers, server_config, options):
            self.launched.append({"server": server.name, "bind": bind, "workers": workers,
                                  "source": source, "options": dict(options)})
            return 0

        self._patches = [
            mock.patch.object(cli, "_load_config", lambda: self.config),
            mock.patch.object(launcher, "launch", fake_launch),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_uses_the_requested_server_and_bind(self):
        code, output = _run(["serve", "--server", "gunicorn",
                             "--bind", "127.0.0.1:9999", "--yes"])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.launched[-1]["server"], "gunicorn")
        self.assertEqual(self.launched[-1]["bind"], "127.0.0.1:9999")
        self.assertEqual(self.launched[-1]["workers"], self.config["server"]["workers"])

    def test_yes_turns_off_the_prompt_and_otherwise_it_is_on(self):
        seen = []

        def fake_choose(requested, configured, interactive):
            seen.append(interactive)
            return _fake_server(), "测试"

        with mock.patch.object(self.launcher, "choose", fake_choose):
            self.assertEqual(_run(["serve", "--yes"])[0], 0)
            self.assertEqual(_run(["serve"])[0], 0)
        self.assertEqual(seen, [False, True])

    def test_unknown_server_is_reported_not_guessed(self):
        code, output = _run(["serve", "--server", "nope", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("nope", output)

    def test_unavailable_server_points_at_the_extra(self):
        fake = _fake_server("waitress", available=False)
        with mock.patch.object(self.launcher, "find", lambda name: fake):
            code, output = _run(["serve", "--server", "waitress", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("elenvind[waitress]", output)



class MissingConfig(unittest.TestCase):
    """没有 config.toml 时必须给出可执行的下一步，而不是堆栈。"""

    def test_serve_explains_how_to_create_an_instance(self):
        from elenvind.core import config as config_module

        with mock.patch.object(config_module, "CONFIG_PATH",
                               Path("/nonexistent/config.toml")):
            code, output = _run(["serve", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("elenvind init", output)

    def test_doctor_reports_it_too(self):
        from elenvind.core import config as config_module

        with mock.patch.object(config_module, "CONFIG_PATH",
                               Path("/nonexistent/config.toml")):
            code, output = _run(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("没有找到配置文件", output)


class LauncherSelection(unittest.TestCase):
    """1.1 的 UX：能自动选就自动选，有多个才问，`--yes` 永远不问。"""

    def setUp(self):
        from elenvind import launcher

        self.launcher = launcher

    def test_configured_server_wins_without_probing(self):
        with mock.patch.object(self.launcher, "detect",
                               side_effect=AssertionError("配置已指定，不该探测")):
            server, reason = self.launcher.choose(None, "gunicorn", interactive=True)
        self.assertEqual(server.name, "gunicorn")
        self.assertEqual(reason, "config.toml")

    def test_asks_only_when_there_is_a_real_choice(self):
        two = [_fake_server("gunicorn"), _fake_server("waitress")]
        with mock.patch.object(self.launcher, "detect", lambda: two), \
             mock.patch("builtins.input", lambda prompt="": "2"):
            server, reason = self.launcher.choose(None, None, interactive=True)
        self.assertEqual(server.name, "waitress")
        self.assertEqual(reason, "用户选择")

    def test_yes_flag_never_asks(self):
        two = [_fake_server("gunicorn"), _fake_server("waitress")]
        with mock.patch.object(self.launcher, "detect", lambda: two), \
             mock.patch("builtins.input", side_effect=AssertionError("不该提问")):
            server, reason = self.launcher.choose(None, None, interactive=False)
        self.assertEqual(server.name, "gunicorn")
        self.assertEqual(reason, "自动选择")

    def test_no_server_at_all_explains_how_to_get_one(self):
        with mock.patch.object(self.launcher, "detect", list):
            server, reason = self.launcher.choose(None, None, interactive=False)
        self.assertIsNone(server)
        self.assertIn("elenvind[gunicorn]", reason)

    def test_every_server_declares_its_tradeoff(self):
        for server in self.launcher.SERVERS:
            self.assertTrue(server.title and server.when, server.name)
            if server.name != "wsgiref":
                self.assertNotEqual(server.extra, "—", server.name)


class LauncherWiring(unittest.TestCase):
    def test_worker_count_resolution_order(self):
        from elenvind import launcher

        self.assertEqual(launcher.resolve_worker_count(3, {"workers": 5})[0], 3)
        self.assertEqual(launcher.resolve_worker_count(None, {"workers": 5})[0], 5)
        automatic, source = launcher.resolve_worker_count(None, {"workers": 0})
        self.assertTrue(1 <= automatic <= launcher.MAX_AUTO_WORKERS)
        self.assertIn("physical core", source)

    def test_trusted_proxies_and_gunicorn_share_one_list(self):
        from elenvind import launcher

        self.assertEqual(launcher._forwarded_allow_ips(
            {"trusted_proxies": ["127.0.0.1", "::1"]}), "127.0.0.1,::1")
        self.assertEqual(launcher._forwarded_allow_ips(
            {"trusted_proxies": "10.0.0.1, 10.0.0.2"}), "10.0.0.1,10.0.0.2")
        self.assertEqual(launcher._forwarded_allow_ips({"trusted_proxies": []}), "")


class NoProxyConfigurationFeature(unittest.TestCase):
    """反代与证书的配置由管理员按手册手写：命令行不提供改动它的能力。"""

    def test_cli_exposes_no_proxy_subcommand(self):
        code, output = _run(["--help"])
        self.assertEqual(code, 0)
        for name in SUBMODULES:
            self.assertIn(name, output)
        self.assertNotIn("proxy", output)
        for argv in (["proxy"], ["proxy", "configure"], ["proxy", "detect"]):
            self.assertEqual(_run(argv)[0], 2, argv)      # 未知子命令 = 用法错误

    def test_reverse_proxy_module_is_read_only(self):
        """该模块只做探测：任何写盘 / 渲染 / 证书检索入口都不该再存在。"""
        from elenvind import reverse_proxy

        for forbidden in ("apply", "plan", "Write", "Plan", "managed_block",
                          "replace_managed_block", "existing_domain_files", "is_ours",
                          "search_certificates", "find_certificate",
                          "certificate_candidates", "verify_and_reload", "run",
                          "Render", "Certificate", "ProxyError", "choose", "find"):
            self.assertFalse(hasattr(reverse_proxy, forbidden),
                             f"{forbidden} 不该再存在（配置由管理员手写）")
        self.assertTrue(hasattr(reverse_proxy, "detect"))

    def test_manual_documents_every_supported_proxy(self):
        """文档是唯一权威：每种代理的写法与证书获取办法都要能在手册里找到。"""
        from elenvind import reverse_proxy

        text = (PROJECT_ROOT / "manual" / "HOSTING.md").read_text(encoding="utf-8")
        for proxy in reverse_proxy.PROXIES:
            self.assertIn(proxy.name, text.lower(), f"手册缺少 {proxy.name} 的写法")
        for topic in ("certbot", "acme.sh", "systemctl reload", "client_max_body_size"):
            self.assertIn(topic, text, f"手册缺少 {topic}")


class LegacyRunner(unittest.TestCase):
    """`run.py` 只做参数翻译；旧命令行的每个用法都要继续成立。"""

    def test_flags_are_translated(self):
        import run

        self.assertEqual(run._translate([]), ["serve"])
        self.assertEqual(run._translate(["--help"]), ["serve", "--help"])
        self.assertEqual(run._translate(["--workers", "4", "--bind", "127.0.0.1:1"]),
                         ["serve", "--workers", "4", "--bind", "127.0.0.1:1"])
        self.assertEqual(run._translate(["--reload", "--access-log"]),
                         ["serve", "--reload", "--access-log"])
        self.assertEqual(run._translate(["--backup"]), ["backup"])
        self.assertEqual(run._translate(["--backup-to", "/tmp/x.db"]),
                         ["backup", "--to", "/tmp/x.db"])

    def test_bad_flags_are_usage_errors(self):
        import run

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            self.assertEqual(run.main(["--bogus"]), 2)
            self.assertEqual(run.main(["--backup-to"]), 2)
            self.assertEqual(run.main(["--backup", "--workers", "2"]), 2)
        self.assertIn("未知选项", buffer.getvalue())

    def test_worker_helpers_are_reexported(self):
        import run
        from elenvind import launcher

        self.assertIs(run.resolve_worker_count, launcher.resolve_worker_count)
        self.assertEqual(run.MAX_AUTO_WORKERS, launcher.MAX_AUTO_WORKERS)


if __name__ == "__main__":
    unittest.main()
