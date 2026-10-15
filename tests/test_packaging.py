"""打包与实例边界：发布出去的东西必须是对的。

版本号只有一个来源、随包的资源真的会进 wheel、站点实例（配置 / 文章 / 数据库）
**绝不**入包、以及"实例根目录"这个新语义真的按文档工作。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT

PACKAGE = PROJECT_ROOT / "elenvind"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


class Metadata(unittest.TestCase):
    def test_version_has_a_single_source(self):
        """version.py 与 __version__ 都必须跟随 pyproject 的 [project].version。"""
        import elenvind
        from elenvind.core import version as version_module

        project = _pyproject()["project"]
        self.assertEqual(version_module.raw_version(), project["version"])
        self.assertEqual(version_module.get_version(), f"v{project['version']}")
        self.assertEqual(elenvind.__version__, project["version"],
                         "__version__ 应是 PEP 440 原值，不带 v 前缀")

    def test_metadata_is_complete_for_pypi(self):
        project = _pyproject()["project"]
        for key in ("name", "version", "description", "readme", "license",
                    "requires-python", "authors", "classifiers", "urls", "dependencies"):
            self.assertIn(key, project, f"元数据缺少 {key}")
        self.assertTrue(project["urls"], "PyPI 页面至少需要一个 URL")
        self.assertTrue((PROJECT_ROOT / project["readme"]).is_file())
        for name in project.get("license-files", []):
            self.assertTrue((PROJECT_ROOT / name).is_file(), name)
        # PEP 639：声明了 license 表达式就不能再放旧的 License 分类器
        self.assertFalse([item for item in project["classifiers"]
                          if item.startswith("License ::")])

    def test_source_version_only_reads_our_own_pyproject(self):
        """向上找 pyproject 时必须认名字：否则 monorepo 里的 venv 会读到别人的版本。"""
        from elenvind.core import version as version_module

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "someone-else"\nversion = "9.9.9"\n',
                encoding="utf-8")
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            self.assertEqual(version_module._source_version(nested), "")

            (root / "pyproject.toml").write_text(
                '[project]\nname = "elenvind"\nversion = "9.9.9"\n', encoding="utf-8")
            self.assertEqual(version_module._source_version(nested), "9.9.9")

    def test_classifiers_exist_in_the_official_trove_list(self):
        """分类器必须是官方 trove 列表里的项。

        PyPI 在**上传时**校验它，写错一个就是 `400 Bad Request`，而前一步的
        `twine check` 不会发现 —— 所以这里提前挡住。本测试在装了
        `trove-classifiers`（CI 会装）时执行，否则跳过：项目的测试依赖仍然只有标准库。
        """
        if importlib.util.find_spec("trove_classifiers") is None:
            self.skipTest("未安装 trove-classifiers（CI 会安装它）")
        from trove_classifiers import classifiers as official

        unknown = [item for item in _pyproject()["project"]["classifiers"]
                   if item not in official]
        self.assertEqual(unknown, [], f"PyPI 会拒绝这些分类器：{unknown}")

    def test_console_script_points_at_the_cli(self):
        scripts = _pyproject()["project"]["scripts"]
        self.assertEqual(scripts.get("elenvind"), "elenvind.cli:main")
        from elenvind import cli

        self.assertTrue(callable(cli.main))

    def test_python_floor_allows_tomllib(self):
        """代码用 `tomllib`（标准库 3.11+）：声明的下限不能更低。"""
        self.assertEqual(_pyproject()["project"]["requires-python"], ">=3.11")


class PackageData(unittest.TestCase):
    def test_shipped_assets_are_declared_and_present(self):
        declared = " ".join(_pyproject()["tool"]["setuptools"]["package-data"]["elenvind"])
        for pattern, directory in (("templates/", PACKAGE / "templates"),
                                   ("i18n/", PACKAGE / "i18n"),
                                   ("resources/", PACKAGE / "resources")):
            self.assertIn(pattern, declared, f"package-data 没有声明 {pattern}")
            self.assertTrue(directory.is_dir(), directory)
            self.assertTrue([item for item in directory.rglob("*") if item.is_file()])

    def test_i18n_is_loaded_from_the_package(self):
        """界面文案属于代码：必须随包分发，不能留在仓库根目录。"""
        from elenvind.core import i18n

        self.assertEqual(i18n._I18N_DIR, PACKAGE / "i18n")
        for lang in ("en", "zh", "ja"):
            self.assertTrue((i18n._I18N_DIR / f"{lang}.toml").is_file(), lang)

    def test_example_config_ships_in_the_package(self):
        example = PACKAGE / "resources" / "config.example.toml"
        self.assertTrue(example.is_file())
        shipped = tomllib.loads(example.read_text(encoding="utf-8"))
        local = tomllib.loads((PROJECT_ROOT / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(set(shipped), set(local),
                         "示例配置与仓库配置的顶层键必须一致（否则守卫会各测各的）")
        self.assertNotIn("templates_dir", shipped,
                         "模板默认在包内：示例配置不该把它指回仓库路径")

    def test_systemd_unit_has_exactly_one_content(self):
        """包内单元（会随 wheel 分发）与 manual/ 下的示例必须逐字节一致。"""
        packaged = PACKAGE / "resources" / "elenvind.service"
        example = PROJECT_ROOT / "manual" / "elenvind.service"
        self.assertEqual(packaged.read_bytes(), example.read_bytes(),
                         "两份 systemd 单元已经漂移，改一处要同步另一处")


class InstanceBoundary(unittest.TestCase):
    def test_package_contains_no_site_data(self):
        for name in ("config.toml", "articles", "custom", "data", "logs", "backups"):
            self.assertFalse((PACKAGE / name).exists(), f"包内不得出现 {name}")

    def test_sdist_excludes_the_site_instance(self):
        manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for line in ("exclude config.toml", "prune articles", "prune custom",
                     "prune data", "prune logs", "prune backups"):
            self.assertIn(line, manifest)
        self.assertNotIn("recursive-include articles", manifest)
        self.assertNotIn("recursive-include custom", manifest)


class InstanceRoot(unittest.TestCase):
    """实例根语义：`ELENVIND_ROOT` → 从当前目录向上找 `config.toml` → 当前目录。"""

    _CODE = ("import sys; sys.path.insert(0, %r); "
             "from elenvind.core.config import ROOT; print(ROOT)")

    def _root(self, cwd: Path, elenvind_root: str | None = None) -> Path:
        environment = {key: value for key, value in os.environ.items()
                       if key != "ELENVIND_ROOT"}
        if elenvind_root is not None:
            environment["ELENVIND_ROOT"] = elenvind_root
        completed = subprocess.run(
            [sys.executable, "-c", self._CODE % str(PROJECT_ROOT)],
            cwd=str(cwd), capture_output=True, text=True, env=environment, check=True)
        return Path(completed.stdout.strip())

    def test_walks_up_to_the_directory_holding_config_toml(self):
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            nested = site / "a" / "b"
            nested.mkdir(parents=True)
            (site / "config.toml").write_text("[server]\n", encoding="utf-8")
            self.assertEqual(self._root(nested), site.resolve())

    def test_falls_back_to_the_current_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "empty"
            empty.mkdir()
            self.assertEqual(self._root(empty), empty.resolve())

    def test_environment_variable_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "empty"
            other = Path(temporary) / "other"
            empty.mkdir()
            other.mkdir()
            (empty / "config.toml").write_text("[server]\n", encoding="utf-8")
            self.assertEqual(self._root(empty, str(other)), other.resolve())


if __name__ == "__main__":
    unittest.main()
