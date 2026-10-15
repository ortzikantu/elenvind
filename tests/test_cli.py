"""CLI 的守卫：子命令可用、危险操作必须显式许可、交互可以被关掉。

CLI 是本项目最容易"看起来能用、实际踩坑"的一块：它写系统配置、启动服务、
还会在没人回答的时候等输入。所以这里把每条分支都钉住，尤其是
"默认只输出、`--apply` 才落地"和"`--yes` 绝不提问"这两条。
"""
from __future__ import annotations

import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import support

SUBMODULES = ("init", "serve", "backup", "doctor", "proxy")


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


class ProxyConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def setUp(self):
        from elenvind import cli, reverse_proxy

        self.cli, self.rp = cli, reverse_proxy
        self.config = support.test_config(support.WORKSPACE)
        self.tls = Path(tempfile.mkdtemp(prefix="elenvind-cli-tls-"))
        (self.tls / "cert.pem").write_text("dummy cert\n", encoding="utf-8")
        (self.tls / "key.pem").write_text("dummy key\n", encoding="utf-8")
        self.patch = mock.patch.object(cli, "_load_config", lambda: self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self._cleanup)
        self.target = self.tls / "out" / "site.conf"

    def _cleanup(self):
        for item in sorted(self.tls.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        self.tls.rmdir()

    def _configure(self, *extra, target=None):
        target = self.target if target is None else target
        with mock.patch.object(self.rp.ReverseProxy, "target",
                              lambda proxy, domain: target):
            return _run(["proxy", "configure", "--proxy", "nginx", "--domain",
                         "example.test", "--cert", str(self.tls / "cert.pem"),
                         "--key", str(self.tls / "key.pem"), *extra])

    def test_detect_needs_no_config(self):
        code, output = _run(["proxy", "detect"])
        self.assertEqual(code, 0)
        self.assertTrue("nginx" in output or "没有检测到" in output)

    def test_configure_refuses_a_missing_certificate(self):
        code, output = _run(["proxy", "configure", "--proxy", "nginx", "--domain",
                             "example.test", "--cert", str(self.tls / "nope.pem"),
                             "--key", str(self.tls / "key.pem")])
        self.assertEqual(code, 1)
        self.assertIn("不存在", output)

    def test_configure_needs_a_domain(self):
        broken = dict(self.config, site_url="")
        with mock.patch.object(self.cli, "_load_config", lambda: broken):
            code, output = _run(["proxy", "configure", "--proxy", "nginx"])
        self.assertEqual(code, 2)
        self.assertIn("域名", output)

    def test_dry_run_renders_but_writes_nothing(self):
        code, output = self._configure()
        self.assertEqual(code, 0, output)
        self.assertFalse(self.target.exists(), "dry-run 不得写任何文件")
        self.assertIn("return 301 https://$host$request_uri", output)
        self.assertIn("proxy_pass http://127.0.0.1:", output)
        self.assertIn("ssl_certificate", output)
        self.assertIn("client_max_body_size", output)
        self.assertIn(str(self.tls / "cert.pem"), output)

    def test_proxy_body_limit_exceeds_the_application_limit(self):
        """代理的上限必须更大，否则应用自己的 413 在代理后面永远看不到。"""
        _code, output = self._configure()
        match = re.search(r"client_max_body_size\s+(\d+)([km]?)", output)
        self.assertIsNotNone(match, "生成的配置里应有 client_max_body_size")
        amount, unit = int(match.group(1)), match.group(2)
        limit = amount * {"": 1, "k": 1024, "m": 1024 * 1024}[unit]
        self.assertGreater(limit, int(self.config["max_body_size"]))

    def test_proxy_read_timeout_exceeds_the_worker_timeout(self):
        from elenvind import reverse_proxy

        read_timeout = reverse_proxy.read_source_timeout({"timeout": 30})
        self.assertGreater(read_timeout, 30)
        self.assertIn(f"proxy_read_timeout    {read_timeout}s", self._configure()[1])

    def test_apply_requires_root(self):
        with mock.patch("os.geteuid", lambda: 1000):
            code, output = self._configure("--apply")
        self.assertEqual(code, 1)
        self.assertIn("root", output)
        self.assertFalse(self.target.exists())

    def test_apply_writes_and_then_reloads(self):
        with mock.patch("os.geteuid", lambda: 0), \
             mock.patch.object(self.rp, "verify_and_reload", return_value=True) as reloaded:
            code, output = self._configure("--apply")
        self.assertEqual(code, 0, output)
        self.assertTrue(self.target.is_file())
        self.assertIn("example.test", self.target.read_text(encoding="utf-8"))
        self.assertEqual(reloaded.call_count, 1)

    def test_apply_reports_a_failed_reload(self):
        with mock.patch("os.geteuid", lambda: 0), \
             mock.patch.object(self.rp, "verify_and_reload", return_value=False):
            code, _output = self._configure("--apply")
        self.assertEqual(code, 1)
        self.assertTrue(self.target.is_file(), "写入成功但 reload 失败：文件应保留现场")

    def test_certificate_lookup_prefers_explicit_paths(self):
        certificate = self.rp.find_certificate("example.test",
                                               str(self.tls / "cert.pem"),
                                               str(self.tls / "key.pem"))
        self.assertEqual(certificate.cert, self.tls / "cert.pem")
        self.assertEqual(certificate.key, self.tls / "key.pem")

    def test_certificate_lookup_never_invents_a_certificate(self):
        with self.assertRaises(self.rp.ProxyError):
            self.rp.find_certificate("no-such-domain.invalid")

    def test_every_proxy_renders_a_complete_configuration(self):
        """四个代理的模板都必须含 443、80 跳转、证书与上游。"""
        certificate = self.rp.Certificate(self.tls / "cert.pem", self.tls / "key.pem")
        spec = self.rp.Render(domain="example.test", upstream="127.0.0.1:6789",
                              certificate=certificate)
        markers = {
            # 不带分号：`listen 443 ssl;` 与 `listen 443 ssl http2;` 都要能匹配
            "nginx": ("listen 80;", "listen 443 ssl"),
            "caddy": ("reverse_proxy", "tls "),
            "apache": ("<VirtualHost *:80>", "<VirtualHost *:443>"),
            "haproxy": ("bind :80", "bind :443 ssl"),
        }
        for proxy in self.rp.PROXIES:
            text = proxy.render(spec)
            self.assertIn("example.test", text, proxy.name)
            self.assertIn("127.0.0.1:6789", text, proxy.name)
            self.assertIn(str(self.tls / "cert.pem"), text, proxy.name)
            for marker in markers[proxy.name]:
                self.assertIn(marker, text, f"{proxy.name}: {marker}")


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
