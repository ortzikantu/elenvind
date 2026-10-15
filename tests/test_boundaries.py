"""1.0 边界守卫：不发出静态文件、错误页一致性、注册关闭、SEO、worker 自适应。

这些用例都对应一次**真实的行为收束**（而不是风格偏好）：
改坏了它们，线上就会以"页面变纯文本 / 表单白填 / 站点结构写死"的形式退化。
"""
from __future__ import annotations

import unittest

from tests import support
from tests.support import ARTICLE_SLUG


class NoStaticServing(unittest.TestCase):
    """本服务不发出静态文件，也不托管任何外部资源。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_resource_paths_are_not_served(self):
        for path in ("/css/style.css", "/imgs/logo.png", "/elenvind/core/config.py"):
            self.assertEqual(support.request("GET", path).status, 404, path)

    def test_single_segment_fallback_does_not_leak_files(self):
        result = support.request("GET", "/config.toml")
        self.assertEqual(result.status, 404)
        self.assertNotIn("site_url", result.text())

    def test_no_static_resource_directory_is_shipped(self):
        from tests.support import PROJECT_ROOT

        for name in ("static", "statics"):
            self.assertFalse((PROJECT_ROOT / "elenvind" / name).exists())


class RouteDeclaration(unittest.TestCase):
    """`permission` 只能收紧权限，不能放宽（防声明式失效开放）。"""

    def test_permission_may_not_weaken_auth(self):
        from elenvind.core.routing import Route

        def handler(request):
            return None

        with self.assertRaises(ValueError):
            Route("/x", ["GET"], handler, auth="required", permission="public")
        with self.assertRaises(ValueError):
            Route("/x", ["GET"], handler, auth="admin", permission="authenticated")

    def test_permission_may_tighten_auth(self):
        from elenvind.core.routing import Route

        def handler(request):
            return None

        Route("/x", ["GET"], handler, auth="required", permission="admin")
        Route("/x", ["GET"], handler, auth="admin", permission="admin")


class ErrorPages(unittest.TestCase):
    """模块抛出的 HttpError 也走带布局、走 i18n 的错误页。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_module_not_found_renders_an_html_page(self):
        session = support.Session()
        session.create_account("Nora", "nora@example.test", "correct-horse-1")
        result = session.post(f"/article/{ARTICLE_SLUG}/comment/delete/999999",
                              {"csrf_token": session.csrf(f"/article/{ARTICLE_SLUG}")})
        self.assertEqual(result.status, 404)
        self.assertIn("text/html", result.header("content-type", ""))
        self.assertNotIn("Comment not found", result.text())


class SiteMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_lists_articles_and_custom_pages(self):
        result = support.request("GET", "/sitemap.xml")
        self.assertEqual(result.status, 200)
        body = result.text()
        self.assertIn(f"/article/{ARTICLE_SLUG}", body)
        self.assertIn("/about", body)          # 自定义页面也必须收录


class CommentI18n(unittest.TestCase):
    """评论区文案由 i18n 提供，模块层不产出面向用户的英文。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_reply_label_comes_from_i18n(self):
        session = support.Session()
        session.create_account("Alice", "alice@example.test", "correct-horse-1")
        self.assertEqual(session.comment(ARTICLE_SLUG, "mine").status, 302)
        parent = support.query("SELECT id FROM comment")[0][0]
        self.assertEqual(session.comment(ARTICLE_SLUG, "reply", reply_to=parent).status, 302)

        page = session.get(f"/article/{ARTICLE_SLUG}").text()
        # 英文站点的期望文案（测试配置 locale=en）；作者标签带 id，故不写死 id
        self.assertRegex(page, r"Replying to @Alice \(#\d+\):")


class RegistrationClosed(unittest.TestCase):
    """关闭注册时**先说清楚**，而不是让访客填完表单再被拒。"""

    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def test_closed_registration_does_not_render_the_form(self):
        from elenvind.core.config import config

        config["registration_enabled"] = False
        try:
            page = support.request("GET", "/register")
            self.assertEqual(page.status, 200)
            self.assertIn("closed", page.text().lower())
            self.assertNotIn('name="password"', page.text())
            self.assertNotIn('name="email"', page.text())
            self.assertNotIn("/register", support.request("GET", "/login").text())
        finally:
            config["registration_enabled"] = True

    def test_open_registration_renders_the_form(self):
        page = support.request("GET", "/register")
        self.assertIn('name="password"', page.text())


class WorkerCount(unittest.TestCase):
    """worker 数：默认按物理核心自适应，显式配置与 CLI 优先。"""

    def test_resolution_order(self):
        from elenvind.core.utils import detect_physical_cores
        from run import MAX_AUTO_WORKERS, resolve_worker_count

        cores = detect_physical_cores()
        self.assertGreaterEqual(cores, 1)
        expected_auto = max(1, min(cores, MAX_AUTO_WORKERS))

        workers, source = resolve_worker_count(None, {})
        self.assertEqual(workers, expected_auto)
        self.assertIn("auto", source)
        # 显式 0 与"没写"等价；正数照用；CLI 覆盖一切
        self.assertEqual(resolve_worker_count(None, {"workers": 0})[0], expected_auto)
        self.assertEqual(resolve_worker_count(None, {"workers": 3})[0], 3)
        self.assertEqual(resolve_worker_count(7, {"workers": 3})[0], 7)


if __name__ == "__main__":
    unittest.main()
