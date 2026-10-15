"""文档 / 配置 / 文案一致性守卫。

`config.toml` 的注释写着"守卫（tests/test_doc_consistency.py）只允许'收窄'：
拒绝裸 `http:` / `https:` / `*`"，包内示例配置引用 `manual/CONFIGURATION.md`
—— 本文件让这些承诺真的成立：配置校验、CSP 收窄、i18n 键一致、交付件存在、
依赖声明与代码实际 import 一致、模板保持 Zero-JS。
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
import unittest
from pathlib import Path

from tests import support
from tests.support import PROJECT_ROOT

TEMPLATES = PROJECT_ROOT / "elenvind" / "templates"
PACKAGE = PROJECT_ROOT / "elenvind"


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


class ConfigValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.ensure_application()

    def _reject(self, **overrides):
        from elenvind.core.config import ConfigError, validate_config

        candidate = support.test_config(support.WORKSPACE)
        candidate.update(overrides)
        with self.assertRaises(ConfigError, msg=f"{overrides} must be rejected"):
            validate_config(candidate)

    def test_rejects_invalid_values(self):
        for overrides in (
            {"locale": "fr"},
            {"title": "   "},
            {"site_url": "https://example.com/"},          # 结尾斜杠
            {"site_url": "ftp://example.com"},             # 非 http(s)
            {"admin_user_id": 0},
            {"admin_user_id": True},
            {"max_length": 0},
            {"max_comment_depth": 0},
            {"session_absolute_days": 99999},
            {"server": {"host": "127.0.0.1", "port": 70000, "workers": 1}},
            {"logging": {"level": "verbose"}},
            {"logging": {"level": "info", "rotate": "yes"}},
            {"static": {"css": "javascript:alert(1)"}},
            {"static": {"hero": "https://x/a.png'); background:url(//evil.test)"}},
            {"static": {"hero": "//evil.test/a.png"}},     # 协议相对
            {"params": {"nav": [{"name": "x", "url": "javascript:alert(1)"}]}},
            {"params": {"social": [{"name": "x", "url": "data:text/html,x"}]}},
            {"security": {"csp": {"script-src": ["*"]}}},          # 裸通配
            {"security": {"csp": {"img-src": ["'self'", "http:"]}}},   # 裸协议
            {"security": {"csp": {"bad name": ["'self'"]}}},
            {"security": {"permissions_policy": "a=(); \r\nX-Evil: 1"}},
        ):
            self._reject(**overrides)

    def test_accepts_narrowing_the_csp(self):
        from elenvind.core.config import validate_config

        candidate = support.test_config(support.WORKSPACE)
        candidate["security"] = dict(candidate["security"])
        candidate["security"]["csp"] = {
            "img-src": ["'self'", "data:", "https://cdn.example.com"],
            "media-src": ["'self'"],
            "object-src": False,
        }
        validate_config(candidate)

    def test_mailto_is_allowed_for_social_links_only(self):
        from elenvind.core.config import validate_config

        candidate = support.test_config(support.WORKSPACE)
        validate_config(candidate)                          # params.social 里的 mailto:
        self._reject(static={"favicon": "mailto:me@example.com"})

    def test_workers_accepts_zero_as_auto(self):
        """`workers = 0` 表示"按物理核心自适应"，是被允许的取值。"""
        from elenvind.core.config import validate_config

        for workers in (0, 1, 8, 64):
            candidate = support.test_config(support.WORKSPACE)
            candidate["server"] = dict(candidate["server"], workers=workers)
            validate_config(candidate)
        self._reject(server={"host": "127.0.0.1", "port": 6789, "workers": 65})

    def test_cookie_prefix_needs_a_way_to_know_it_is_https(self):
        """`__Host-` 前缀要求 Secure；若应用无从得知自己跑在 HTTPS 上，直接拒绝启动。

        否则表现为"能登录，但下一个请求又变回未登录"——浏览器丢弃了 Cookie，
        而启动日志里什么也看不出来。
        """
        from elenvind.core.config import validate_config

        candidate = support.test_config(support.WORKSPACE)   # site_url 已是 https
        candidate["server"] = dict(candidate["server"], cookie_prefix=True)
        validate_config(candidate)
        self._reject(site_url="http://test.invalid", server={
            "host": "127.0.0.1", "port": 6789, "workers": 0,
            "trusted_proxies": [], "cookie_prefix": True})

    def test_shipped_configs_are_valid(self):
        """仓库自带的 config.toml 与 config.example.toml 都必须能启动。

        `config.toml` 允许被部署者删除（只留 example），此时跳过而不是失败 ——
        测试不该依赖"恰好存在的文件"。
        """
        from elenvind.core.config import validate_config

        names = ["elenvind/resources/config.example.toml"]
        if (PROJECT_ROOT / "config.toml").is_file():
            names.append("config.toml")
        for name in names:
            validate_config(_load_toml(PROJECT_ROOT / name))

    @unittest.skipUnless((PROJECT_ROOT / "config.toml").is_file(), "config.toml not present")
    def test_example_and_shipped_config_agree_on_keys(self):
        """两份配置的键集合应当一致（避免示例文档漂移成"另一套配置"）。"""
        shipped = _load_toml(PROJECT_ROOT / "config.toml")
        example = _load_toml(PROJECT_ROOT / "elenvind" / "resources" / "config.example.toml")
        missing = sorted(set(shipped) - set(example))
        self.assertEqual(missing, [], f"example config is missing top-level keys: {missing}")


class I18nConsistency(unittest.TestCase):
    def test_all_languages_define_the_same_keys(self):
        tables = {lang: _load_toml(PROJECT_ROOT / "elenvind" / "i18n" / f"{lang}.toml")
                  for lang in ("en", "zh", "ja")}
        keys = {lang: set(table) for lang, table in tables.items()}
        self.assertEqual(keys["en"], keys["zh"], "zh and en key sets differ")
        self.assertEqual(keys["en"], keys["ja"], "ja and en key sets differ")

    def test_every_key_used_in_code_or_templates_exists(self):
        declared = set(_load_toml(PROJECT_ROOT / "elenvind" / "i18n" / "en.toml"))
        used: set[str] = set()

        # .py：用 AST 只看真正的 t(...) 调用（docstring 里的 `t("key")` 示例不算）
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "t"):
                    used |= {arg.value for arg in node.args
                             if isinstance(arg, ast.Constant) and isinstance(arg.value, str)}

        # .html：模板里只有 `t('key')` 这一种形态
        template_pattern = re.compile(r"""\bt\(\s*['"]([a-z0-9_]+)['"]""")
        for path in TEMPLATES.rglob("*.html"):
            used |= set(template_pattern.findall(path.read_text(encoding="utf-8")))

        missing = sorted(used - declared)
        self.assertEqual(missing, [], f"i18n keys referenced but not defined: {missing}")


#: 核心运行时依赖：`pip install elenvind` 就要装上的东西，也是 requirements.txt 的内容
RUNTIME_DEPS = frozenset({"jinja2", "markdown", "markupsafe"})
#: 可选的 WSGI 服务器：由 `pip install elenvind[<extra>]` 提供，**不得**成为硬依赖，
#: 也**不得**在模块顶层导入（否则"可选"就名不副实）
OPTIONAL_SERVERS = frozenset({"gunicorn", "waitress", "uwsgi", "hypercorn", "daphne"})


class Deliverables(unittest.TestCase):
    def test_package_ships_no_static_assets(self):
        """边界：本服务不发出静态文件，包内不得再出现静态资源目录。"""
        for name in ("static", "statics"):
            self.assertFalse((PACKAGE / name).exists(),
                             f"包内不应存在静态资源目录 {name}/")

    def test_readme_and_configuration_doc_exist(self):
        self.assertTrue((PROJECT_ROOT / "README.md").is_file())
        self.assertTrue((PROJECT_ROOT / "manual" / "CONFIGURATION.md").is_file())

    def test_deployment_examples_exist(self):
        for name in ("nginx.conf.example", "elenvind.service", "logrotate.conf"):
            self.assertTrue((PROJECT_ROOT / "manual" / name).is_file(), name)

    @staticmethod
    def _imported_names(node) -> set[str]:
        if isinstance(node, ast.Import):
            return {alias.name.split(".")[0] for alias in node.names}
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            return {node.module.split(".")[0]}
        return set()

    def _third_party_imports(self):
        """扫包内与 run.py，返回 `(全部第三方导入, 仅模块顶层的第三方导入)`。"""
        everything: set[str] = set()
        top_level: set[str] = set()
        for path in [*PACKAGE.rglob("*.py"), PROJECT_ROOT / "run.py"]:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                everything |= self._imported_names(node)
            for node in tree.body:
                top_level |= self._imported_names(node)
        keep = lambda name: (name not in sys.stdlib_module_names        # noqa: E731
                             and name not in {"elenvind", "__future__"})
        return {name for name in everything if keep(name)}, \
            {name for name in top_level if keep(name)}

    def test_requirements_cover_third_party_imports(self):
        everything, top_level = self._third_party_imports()
        self.assertEqual(everything - OPTIONAL_SERVERS, RUNTIME_DEPS,
                         "the dependency set changed; update requirements.txt and this test")
        self.assertEqual(top_level & OPTIONAL_SERVERS, set(),
                         "WSGI servers must be imported lazily, otherwise "
                         "`pip install elenvind` would hard-require one of them")

        declared = {line.split("==")[0].strip().lower()
                    for line in (PROJECT_ROOT / "requirements.txt").read_text(
                        encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")}
        self.assertTrue(declared >= RUNTIME_DEPS,
                        f"requirements.txt is missing packages: {declared}")

    def test_optional_servers_are_declared_as_extras(self):
        """每个可选服务器都要有对应的 pip extra，否则 CLI 的引导指向不存在的包。"""
        import tomllib

        pyproject = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        extras = pyproject["project"]["optional-dependencies"]
        everything, _top_level = self._third_party_imports()
        for name in sorted(everything & OPTIONAL_SERVERS):
            self.assertIn(name, extras, f"pyproject extras 缺少 {name}")
            self.assertTrue(any(dep.lower().startswith(name.lower())
                                for dep in extras[name]),
                            f"extra {name} 没声明对应依赖：{extras[name]}")
        # 反向保证：可选服务器绝不能出现在必装依赖里
        required = " ".join(pyproject["project"]["dependencies"]).lower()
        for name in sorted(OPTIONAL_SERVERS):
            self.assertNotIn(name, required,
                             f"{name} 是可选服务器，不该出现在 [project].dependencies")

    def test_templates_stay_zero_javascript(self):
        """CSP 声明 script-src 'none'：模板里就不该出现脚本或 javascript: URL。"""
        for path in TEMPLATES.rglob("*.html"):
            source = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("<script", source, f"{path.name} contains a script tag")
            self.assertNotIn("javascript:", source, f"{path.name} contains javascript:")

    #: 明说"以前怎么做的"的说法：注释与手册只讲是什么 / 怎么做 / 为什么
    HISTORY_PHRASES = (
        "曾经", "此前", "早期版本", "旧实现", "旧方案", "历史实现", "历史契约",
        "历史命名", "本次修复", "修复前", "已撤回", "was previously", "used to be",
    )

    def test_code_and_manuals_carry_no_change_history(self):
        """注释与手册只描述现状，不写"以前是那样做的"。"""
        offenders = []
        files = [*PACKAGE.rglob("*.py"), *sorted(PROJECT_ROOT.glob("tests/*.py")),
                 *sorted(PROJECT_ROOT.glob("manual/*.md")), PROJECT_ROOT / "README.md"]
        for path in files:
            if path.name == "test_doc_consistency.py":     # 本文件持有词表，跳过自身
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for phrase in self.HISTORY_PHRASES:
                    if phrase in line:
                        offenders.append(f"{path}:{number}: {line.strip()[:70]}")
        self.assertEqual(offenders, [],
                         "注释/手册里出现了变更历史叙述；请改写成现状描述"
                         "（历史写在提交信息与发布说明里）")

    def test_only_linux_posix_is_claimed(self):
        """平台声明只有 Linux/POSIX：写事务依赖 fcntl.flock，声明别的平台就是撒谎。"""
        import tomllib

        pyproject = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        systems = [item for item in pyproject["project"]["classifiers"]
                   if item.startswith("Operating System ::")]
        self.assertEqual(systems, ["Operating System :: POSIX :: Linux"])

        offenders = []
        files = [*PACKAGE.rglob("*.py"), *sorted(PROJECT_ROOT.glob("manual/*.md")),
                 PROJECT_ROOT / "README.md"]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for word in ("macOS", "MacOS", "Windows", "跨平台"):
                if word in text:
                    offenders.append(f"{path}: {word}")
        self.assertEqual(offenders, [], "只支持 Linux/POSIX，不应出现其它平台声明")

    def test_no_test_referenced_by_comments_is_missing(self):
        """注释里点名的守卫测试必须真的存在（否则又是"文档说谎"）。"""
        referenced: set[str] = set()
        sources = [*PACKAGE.rglob("*.py"), PROJECT_ROOT / "config.toml",
                   PROJECT_ROOT / "elenvind" / "resources" / "config.example.toml"]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            referenced |= set(re.findall(r"tests/([a-z_]+\.py)", text))
        missing = sorted(name for name in referenced
                         if not (PROJECT_ROOT / "tests" / name).is_file())
        self.assertEqual(missing, [], f"comments reference missing test files: {missing}")


if __name__ == "__main__":
    unittest.main()
