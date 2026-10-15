"""日志卫生与代理信任。

对应 `core/app.py` 里"访问日志**不记**查询串 / 请求体 / Cookie / 凭据"的承诺
（注释说这些由 `tests/test_logging_proxy.py` 断言），以及 `core/utils.py` 里
"只有受信对端才能决定客户端 IP 与 scheme"的安全边界。
"""
from __future__ import annotations

import logging
import unittest

from tests import support
from tests.support import ARTICLE_SLUG


class LogHygiene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def setUp(self):
        support.reset_database()

    def _flush(self) -> None:
        for handler in logging.getLogger().handlers:
            handler.flush()

    def test_credentials_and_query_strings_never_reach_the_log(self):
        password = "correct-horse-1"
        session = support.Session()
        session.create_account("Logger", "logger@example.test", password)
        token = session.csrf("/login")
        session.get("/?page=2&token=secret")          # 查询串不得进日志
        session.comment(ARTICLE_SLUG, "hello")

        self._flush()
        text = support.WORKSPACE.log.read_text(encoding="utf-8")
        self.assertIn("Request handled", text, "the access log should be written")
        self.assertNotIn(password, text)
        self.assertNotIn(token, text)
        self.assertNotIn("page=2", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("csrf_token", text)

    def test_access_log_fields_are_escaped_and_truncated(self):
        from elenvind.core.app import _log_field

        self.assertEqual(_log_field("/a\nFAKE"), "/a\\x0aFAKE")
        self.assertEqual(_log_field("/x\ty"), "/x\\x09y")
        self.assertEqual(_log_field("/caf\u00e9"), "/caf\\xe9")
        self.assertTrue(_log_field("A" * 500).endswith("…")
                        and len(_log_field("A" * 500)) <= 201)


class ProxyTrust(unittest.TestCase):
    """X-Forwarded-* 只在直连对端是受信代理时才被采信，且取**最右**有效跳。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_untrusted_peer_cannot_spoof_the_client_ip(self):
        from elenvind.core.utils import get_client_ip

        self.assertEqual(get_client_ip({}, "8.8.8.8"), "8.8.8.8")
        self.assertEqual(get_client_ip({"x-forwarded-for": "1.2.3.4"}, "8.8.8.8"), "8.8.8.8")
        self.assertEqual(get_client_ip({"x-forwarded-for": "not-an-ip"}, "8.8.8.8"), "8.8.8.8")

    def test_trusted_proxy_takes_the_rightmost_untrusted_hop(self):
        from elenvind.core.utils import get_client_ip

        # 覆盖式：只剩真实客户端
        self.assertEqual(get_client_ip({"x-forwarded-for": "203.0.113.9"}, "127.0.0.1"),
                         "203.0.113.9")
        # 追加式：左侧是客户端自己塞的，必须取右侧那一跳
        self.assertEqual(
            get_client_ip({"x-forwarded-for": "1.2.3.4, 203.0.113.9"}, "127.0.0.1"),
            "203.0.113.9")
        self.assertEqual(
            get_client_ip({"x-forwarded-for": "9.9.9.9, 127.0.0.1"}, "127.0.0.1"),
            "9.9.9.9")

    def test_scheme_precedence(self):
        """scheme 判定：site_url 声明 https 时一律 https；否则只有受信对端能升到 https。

        这条守卫直接对应两类真实的代理差异：CDN→源站是明文时代理"诚实地"写出
        `http`，以及最简 TCP 转发器不覆写请求头时客户端自己塞
        `X-Forwarded-Proto: https`。
        """
        from elenvind.core.config import config
        from elenvind.core.utils import get_request_scheme

        trusted = {"wsgi.url_scheme": "http", "REMOTE_ADDR": "127.0.0.1"}
        remote = {"wsgi.url_scheme": "http", "REMOTE_ADDR": "8.8.8.8"}
        # 1) site_url 声明 https（测试配置）：不看对端、不被任何头降级
        self.assertEqual(get_request_scheme(trusted, {}), "https")
        self.assertEqual(
            get_request_scheme(trusted, {"x-forwarded-proto": "http"}), "https")
        self.assertEqual(get_request_scheme(remote, {}), "https")

        # 2) 声明为 http 时：只有受信对端的 XFP 能把这次请求升到 https
        config["site_url"] = "http://test.invalid"
        try:
            self.assertEqual(get_request_scheme(trusted, {}), "http")
            self.assertEqual(
                get_request_scheme(trusted, {"x-forwarded-proto": "https"}), "https")
            # 非受信对端说 https 一律不采信（防公网直连者伪造 scheme）
            self.assertEqual(
                get_request_scheme(remote, {"x-forwarded-proto": "https"}), "http")
            self.assertEqual(get_request_scheme(trusted, {"x-forwarded-proto": "gopher"}),
                             "http")
        finally:
            config["site_url"] = "https://test.invalid"

    def test_https_requests_get_hsts_and_secure_cookies(self):
        result = support.request("GET", "/login", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(result.header("strict-transport-security"), "max-age=300")
        self.assertTrue(any("Secure" in value for value in result.all_headers("set-cookie")))

    def test_declared_https_gets_hsts_regardless_of_the_proxy(self):
        """`site_url` 声明 https 时，Secure / HSTS 不依赖代理发不发头、也不依赖对端。

        这是"换掉任何反代都不用改应用"的前提：最简 TCP 转发器（不发头）、
        nginx（发 `X-Forwarded-Proto: http`）都必须得到同一个结论。
        """
        for kwargs in ({}, {"ip": "8.8.8.8"},
                       {"headers": {"X-Forwarded-Proto": "http"}}):
            result = support.request("GET", "/login", **kwargs)
            self.assertEqual(result.header("strict-transport-security"), "max-age=300",
                             kwargs)
            self.assertTrue(
                any("Secure" in value for value in result.all_headers("set-cookie")),
                kwargs)

    def test_plain_http_declaration_gets_no_hsts(self):
        """`site_url` 声明 http 且对端不受信时，不得下发 HSTS / Secure。"""
        from elenvind.core.config import config

        config["site_url"] = "http://test.invalid"
        try:
            result = support.request("GET", "/login", ip="8.8.8.8")
            self.assertIsNone(result.header("strict-transport-security"))
            self.assertFalse(
                any("Secure" in value for value in result.all_headers("set-cookie")))
        finally:
            config["site_url"] = "https://test.invalid"

    def test_host_header_cannot_poison_absolute_urls(self):
        result = support.request("GET", "/sitemap.xml", headers={"Host": "evil.test"})
        self.assertNotIn("evil.test", result.text())
        self.assertIn("https://test.invalid", result.text())


if __name__ == "__main__":
    unittest.main()
