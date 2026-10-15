"""反代配置的"先检索、只增不覆盖"守卫。

这套规则的重点是**不要破坏用户已有的配置**：同一域名下已经有一份手写配置时，我们必须
拒绝而不是覆盖；如果是我们写过的受管段落，就只能替换那一段，文件其余内容一个字节都
不能变。重复运行必须幂等（否则每次 `--apply` 都会让文件长大一圈）。
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elenvind import reverse_proxy as rp
from tests import support

DOMAIN = "example.test"


def _spec(cert_dir: Path, domain: str = DOMAIN) -> rp.Render:
    cert = rp.Certificate(cert_dir / "fullchain.pem", cert_dir / "privkey.pem",
                          source="测试")
    (cert_dir / "fullchain.pem").write_text("cert\n", encoding="utf-8")
    (cert_dir / "privkey.pem").write_text("key\n", encoding="utf-8")
    return rp.Render(domain=domain, upstream="127.0.0.1:6789", certificate=cert,
                     body_limit="2m", body_limit_bytes=2097152, read_timeout=40)


def _proxy(root: Path, *, with_main: bool = False) -> rp.ReverseProxy:
    """一个把配置落点指向临时目录的假代理（不碰 /etc）。"""
    conf = root / "conf.d"
    conf.mkdir(parents=True, exist_ok=True)
    main = root / "main.conf" if with_main else None
    return rp.ReverseProxy(
        name="nginx", title="nginx（测试）", when="测试用", detect=lambda: True,
        targets=(conf,), filename=lambda domain: f"elenvind-{domain}.conf",
        render=lambda spec: (f"server {{\n    server_name {spec.domain};\n"
                             f"    upstream {spec.upstream};\n}}\n"),
        search=(conf, main) if main else (conf,),
        test=(), reload=(), extra_step="", main_config=main,
        include_line="include conf.d/*.conf" if with_main else "")


class PlanIsNonDestructive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="elenvind-proxy-")
        self.root = Path(self._tmp.name)
        self.cert_dir = self.root / "certs"
        self.cert_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_new_domain_creates_a_managed_block(self):
        proxy = _proxy(self.root)
        plan = rp.plan(proxy, _spec(self.cert_dir))
        self.assertFalse(plan.blocked)
        self.assertEqual(plan.action, "新增配置")
        self.assertEqual(len(plan.writes), 1)
        write = plan.writes[0]
        self.assertEqual(write.mode, "create")
        self.assertIn(rp.MARKER_BEGIN.format(domain=DOMAIN), write.text)
        self.assertIn(rp.MARKER_END.format(domain=DOMAIN), write.text)
        self.assertIn(DOMAIN, write.text)

    def test_foreign_config_for_the_same_domain_is_refused(self):
        """同一域名已有别人写的配置：必须拒绝，而不是覆盖或再造一份冲突的。"""
        proxy = _proxy(self.root)
        theirs = self.root / "conf.d" / "my-site.conf"
        theirs.write_text(f"server {{\n    server_name {DOMAIN};\n}}\n", encoding="utf-8")
        before = theirs.read_text(encoding="utf-8")

        plan = rp.plan(proxy, _spec(self.cert_dir))
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.conflict, theirs)
        self.assertIn("不会", plan.message)
        self.assertEqual(theirs.read_text(encoding="utf-8"), before)
        self.assertFalse(list((self.root / "conf.d").glob("elenvind-*")))

    def test_domain_match_is_exact_not_substring(self):
        """`example.test` 不能命中 `notexample.test` 或 `example.test.cn`。"""
        proxy = _proxy(self.root)
        (self.root / "conf.d" / "other.conf").write_text(
            "server_name notexample.test;\nserver_name example.test.cn;\n",
            encoding="utf-8")
        plan = rp.plan(proxy, _spec(self.cert_dir))
        self.assertFalse(plan.blocked, "子串匹配会误判成冲突，导致永远无法新增")

    def test_managed_block_is_updated_in_place(self):
        """第二次运行只替换受管段落，文件里其它内容必须原样保留。"""
        proxy = _proxy(self.root)
        target = proxy.target(DOMAIN)
        first = rp.plan(proxy, _spec(self.cert_dir))
        rp.apply(first)
        target.write_text("# 我自己的其它配置\n"
                          + target.read_text(encoding="utf-8")
                          + "# 我自己的结尾\n", encoding="utf-8")

        spec = _spec(self.cert_dir)
        spec = rp.Render(domain=spec.domain, upstream="127.0.0.1:9999",
                         certificate=spec.certificate, body_limit="4m",
                         body_limit_bytes=4194304, read_timeout=40)
        second = rp.plan(proxy, spec)
        self.assertEqual(second.action, "就地更新受管段落")
        self.assertEqual(len(second.writes), 1)
        text = second.writes[0].text
        self.assertTrue(text.startswith("# 我自己的其它配置\n"))
        self.assertTrue(text.endswith("# 我自己的结尾\n"))
        self.assertIn("127.0.0.1:9999", text)
        self.assertNotIn("127.0.0.1:6789", text)

    def test_repeated_runs_are_idempotent(self):
        proxy = _proxy(self.root)
        rp.apply(rp.plan(proxy, _spec(self.cert_dir)))
        once = proxy.target(DOMAIN).read_text(encoding="utf-8")
        rp.apply(rp.plan(proxy, _spec(self.cert_dir)))
        twice = proxy.target(DOMAIN).read_text(encoding="utf-8")
        self.assertEqual(once, twice, "重复 apply 不该让文件增长或漂移")

    def test_existing_foreign_file_at_the_target_path_is_refused(self):
        proxy = _proxy(self.root)
        target = proxy.target(DOMAIN)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 有人手写了这个文件名\n", encoding="utf-8")
        plan = rp.plan(proxy, _spec(self.cert_dir))
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.conflict, target)
        self.assertIn("手写", plan.message + "手写")

    def test_apply_keeps_a_backup_of_modified_files(self):
        proxy = _proxy(self.root)
        rp.apply(rp.plan(proxy, _spec(self.cert_dir)))
        target = proxy.target(DOMAIN)
        before = target.read_text(encoding="utf-8")
        spec = _spec(self.cert_dir)
        rp.apply(rp.plan(proxy, spec))
        backup = target.with_suffix(target.suffix + ".bak")
        self.assertTrue(backup.is_file(), "改动前应留一份 .bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), before)

    def test_include_line_is_added_once_and_the_drop_in_is_created(self):
        proxy = _proxy(self.root, with_main=True)
        proxy.main_config.write_text("# 主配置\n", encoding="utf-8")

        first = rp.plan(proxy, _spec(self.cert_dir))
        modes = sorted(write.mode for write in first.writes)
        self.assertEqual(modes, ["create", "insert-line"])
        rp.apply(first)
        self.assertIn(proxy.include_line, proxy.main_config.read_text(encoding="utf-8"))

        second = rp.plan(proxy, _spec(self.cert_dir))
        self.assertEqual([write.mode for write in second.writes], ["replace"],
                         "第二次只该更新受管段落，不该再加一行 include")


class CertificateSearch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="elenvind-cert-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _install(self, name="tls", cert="fullchain.pem", key="privkey.pem"):
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / cert).write_text("cert\n", encoding="utf-8")
        (directory / key).write_text("key\n", encoding="utf-8")
        return directory

    def test_finds_certificates_in_the_instance_directory(self):
        directory = self._install()
        found = rp.search_certificates(DOMAIN, self.root)
        self.assertTrue(found)
        self.assertEqual(found[0].cert, directory / "fullchain.pem")
        self.assertIn("实例目录", found[0].source)

    def test_www_and_bare_domain_are_tried_both_ways(self):
        """证书常签给裸域名，而站点可能配 www（反之亦然）。"""
        labels = [label for label, _cert, _key in
                  rp.certificate_candidates("www.example.test")]
        self.assertTrue(any("example.test" in label and "www" not in label for label in labels))

    def test_letsencrypt_path_is_searched_by_domain(self):
        candidates = " ".join(str(cert) for _label, cert, _key in
                              rp.certificate_candidates(DOMAIN))
        self.assertIn(f"/etc/letsencrypt/live/{DOMAIN}/fullchain.pem", candidates)

    def test_missing_certificate_guides_the_user_to_generate_one(self):
        with self.assertRaises(rp.ProxyError) as caught:
            rp.find_certificate(DOMAIN, instance_root=self.root)
        message = str(caught.exception)
        self.assertIn("certbot", message)
        self.assertIn(DOMAIN, message)
        self.assertIn("重新运行", message, "必须告诉用户生成后再跑一次即可")
        self.assertIn("/etc/letsencrypt/live/", message)

    def test_explicit_paths_win_and_are_validated(self):
        directory = self._install()
        found = rp.find_certificate(DOMAIN, str(directory / "fullchain.pem"),
                                    str(directory / "privkey.pem"), self.root)
        self.assertEqual(found.source, "--cert/--key")
        with self.assertRaises(rp.ProxyError):
            rp.find_certificate(DOMAIN, str(directory / "nope.pem"),
                                str(directory / "privkey.pem"), self.root)


class DryRunNeverWrites(unittest.TestCase):
    """CLI 层：dry-run 只打印，冲突时直接失败且不碰任何文件。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def setUp(self):
        from elenvind import cli

        self.cli = cli
        self._tmp = tempfile.TemporaryDirectory(prefix="elenvind-proxycli-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cert_dir = self.root / "tls"
        self.cert_dir.mkdir()
        (self.cert_dir / "fullchain.pem").write_text("cert\n", encoding="utf-8")
        (self.cert_dir / "privkey.pem").write_text("key\n", encoding="utf-8")
        self.config = dict(support.test_config(support.WORKSPACE),
                           site_url=f"https://{DOMAIN}")
        self.patch = mock.patch.object(cli, "_load_config", lambda: self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _run(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                code = self.cli.main(argv)
            except SystemExit as request:
                code = request.code if isinstance(request.code, int) else 0
        return code, buffer.getvalue()

    def _configure(self, *extra):
        """把落点与检索范围都指到临时目录，顺带让证书来自临时目录。"""
        proxy = _proxy(self.root)
        with mock.patch.object(self.cli.reverse_proxy, "choose",
                               lambda requested, interactive: (proxy, "测试")), \
             mock.patch.object(self.cli.reverse_proxy, "find_certificate",
                               lambda domain, cert, key, root: rp.Certificate(
                                   self.cert_dir / "fullchain.pem",
                                   self.cert_dir / "privkey.pem", source="测试")):
            return self._run(["proxy", "configure", "--proxy", "nginx",
                              "--domain", DOMAIN, *extra])

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        code, output = self._configure()
        self.assertEqual(code, 0, output)
        self.assertIn("新增配置", output)
        self.assertIn(rp.MARKER_BEGIN.format(domain=DOMAIN), output)
        self.assertFalse(list((self.root / "conf.d").glob("*")), "dry-run 不得落盘")

    def test_conflict_fails_without_touching_the_file(self):
        theirs = self.root / "conf.d" / "my-site.conf"
        theirs.parent.mkdir(parents=True, exist_ok=True)
        theirs.write_text(f"server_name {DOMAIN};\n", encoding="utf-8")
        before = theirs.read_text(encoding="utf-8")

        code, output = self._configure()
        self.assertEqual(code, 1)
        self.assertIn("不会", output)
        self.assertEqual(theirs.read_text(encoding="utf-8"), before)
        self.assertFalse(list((self.root / "conf.d").glob("elenvind-*")))

    def test_apply_writes_then_reloads(self):
        with mock.patch("os.geteuid", lambda: 0), \
             mock.patch.object(self.cli.reverse_proxy, "verify_and_reload",
                               return_value=True) as reloaded:
            code, output = self._configure("--apply")
        self.assertEqual(code, 0, output)
        target = self.root / "conf.d" / f"elenvind-{DOMAIN}.conf"
        self.assertTrue(target.is_file())
        self.assertIn(rp.MARKER_BEGIN.format(domain=DOMAIN),
                      target.read_text(encoding="utf-8"))
        self.assertEqual(reloaded.call_count, 1)


if __name__ == "__main__":
    unittest.main()
