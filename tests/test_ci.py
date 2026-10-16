"""CI 与发布流水线的机械守卫。

流水线是最容易"写错了却没人发现"的地方：它不在测试里跑，只有真正打 tag 那一刻才暴露。
这里做**纯文本扫描**（不引入 YAML 依赖），把关键约定钉住：只跑 Linux、门禁命令与文档一致、
发布只认 tag 且校验版本、认证走 OIDC 而不是把 token 写进仓库。
"""
from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT

WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
PUBLISH = WORKFLOWS / "publish.yml"
RELEASE_DOC = PROJECT_ROOT / "manual" / "RELEASE.md"

#: 明文凭据的模样（真正该出现的是 `${{ secrets.* }}` 或 OIDC）
SECRET_PATTERNS = (
    re.compile(r"^\s*password:\s*\S", re.M),
    re.compile(r"pypi-AgEI"),
    re.compile(r"TWINE_PASSWORD\s*[:=]\s*\S"),
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class Workflows(unittest.TestCase):
    def test_workflow_files_exist(self):
        self.assertTrue(CI.is_file(), "缺少 .github/workflows/ci.yml")
        self.assertTrue(PUBLISH.is_file(), "缺少 .github/workflows/publish.yml")

    def test_only_linux_runners(self):
        """应用只支持提供 fcntl.flock 的 POSIX 系统，CI 不该跑别的平台。"""
        for path in WORKFLOWS.glob("*.yml"):
            text = _text(path)
            for runner in re.findall(r"runs-on:\s*(\S+)", text):
                self.assertEqual(runner, "ubuntu-latest", f"{path.name}: {runner}")
            for other in ("windows-latest", "macos-latest", "windows-", "macos-"):
                self.assertNotIn(other, text, f"{path.name} 出现了非 Linux runner")

    def test_ci_runs_the_documented_gates(self):
        text = _text(CI)
        self.assertIn("ruff check .", text)
        self.assertIn("python -m unittest discover -s tests -t .", text)
        # 冒烟必须验证"不发出静态文件"这条边界与启动信息
        self.assertIn("/css/style.css", text)
        self.assertIn("TLS is terminated by the reverse proxy", text)

    def test_ci_matrix_matches_declared_python_support(self):
        """声明的每个 Python 版本都要真的被测；也不能去测低于下限的版本。"""
        project = tomllib.loads(_text(PROJECT_ROOT / "pyproject.toml"))["project"]
        floor = tuple(int(part) for part in
                      project["requires-python"].lstrip(">= ").split(".")[:2])
        ci_text = _text(CI)
        tested = set(re.findall(r'"(\d+\.\d+)"', ci_text))
        self.assertTrue(tested, "CI 矩阵里没有 Python 版本")
        self.assertIn(".".join(str(part) for part in floor), tested,
                      "CI 没有覆盖声明的版本下限")
        for version in tested:
            major, minor = (int(part) for part in version.split("."))
            self.assertGreaterEqual((major, minor), floor,
                                    f"CI 跑了低于 requires-python 下限的 {version}")

        declared = [item.rsplit(" ", 1)[-1] for item in project["classifiers"]
                    if item.startswith("Programming Language :: Python :: 3.")]
        for version in declared:
            self.assertIn(f'"{version}"', ci_text, f"声明支持 {version}，但 CI 不测它")

    def test_both_workflows_provision_the_same_nginx(self):
        """CI 与发布流水线必须用同一个脚本装 nginx。

        两边环境不一致正好是一次真实故障：`ci.yml` 装了新版 nginx，而发布流水线的
        verify 任务没装 —— 它用 runner 自带的 1.24 跑同一套测试，于是 `http2 on;`
        被报成未知指令，发布被挡在测试这一步。
        """
        for path in (CI, PUBLISH):
            self.assertIn(".github/scripts/install-nginx.sh", _text(path), path.name)

    def test_nginx_install_script_enforces_a_modern_version(self):
        script = PROJECT_ROOT / ".github" / "scripts" / "install-nginx.sh"
        self.assertTrue(script.is_file(), "缺少装 nginx 的脚本")
        text = script.read_text(encoding="utf-8")
        self.assertIn("nginx.org/keys", text)
        self.assertIn("(1, 25, 1)", text, "必须断言版本：装不上就会静默退回旧 nginx")
        self.assertIn("set -euo pipefail", text)

    def test_publish_is_tag_driven_and_checks_the_version(self):
        text = _text(PUBLISH)
        self.assertIn("tags:", text, "发布必须由 tag 触发")
        self.assertNotIn("branches:", text, "发布会把任意提交发上 PyPI")
        self.assertIn("pyproject.toml", text, "发布前必须读 pyproject 校验版本")
        self.assertIn("GITHUB_REF_NAME", text)

    def test_publish_uses_oidc_and_never_stores_a_token(self):
        text = _text(PUBLISH)
        self.assertIn("id-token: write", text, "Trusted Publishing 需要 OIDC 权限")
        self.assertIn("pypa/gh-action-pypi-publish", text)
        self.assertIn("environment: pypi", text)
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(pattern.search(text),
                              f"工作流里出现了明文凭据：{pattern.pattern}")

    def test_ci_never_uses_secrets(self):
        """测试与静态检查不需要任何凭据；出现凭据说明设计跑偏了。"""
        text = _text(CI)
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(pattern.search(text))
        self.assertNotIn("secrets.", text)

    def test_publish_attaches_artifacts_to_a_release(self):
        text = _text(PUBLISH)
        self.assertIn("gh release create", text)
        self.assertIn("upload-artifact", text)
        self.assertIn("download-artifact", text)

    def test_release_doc_exists_and_is_linked(self):
        self.assertTrue(RELEASE_DOC.is_file(), "缺少 manual/RELEASE.md")
        self.assertIn("manual/RELEASE.md", _text(PROJECT_ROOT / "README.md"))
        self.assertIn("(RELEASE.md)", _text(PROJECT_ROOT / "manual" / "README.md"))
        doc = _text(RELEASE_DOC)
        # 手册里写的 PyPI 登记字段必须与工作流一致
        self.assertIn("publish.yml", doc)
        self.assertIn("environment", doc.lower())
        self.assertIn("Trusted Publishing", doc)

    def test_repositories_are_documented(self):
        """Codeberg 是主仓库，GitHub 是镜像 + 构建仓：这条要写在元数据里。"""
        urls = tomllib.loads(_text(PROJECT_ROOT / "pyproject.toml"))["project"]["urls"]
        self.assertIn("codeberg.org", urls["Homepage"])
        self.assertIn("codeberg.org", urls["Repository"])
        self.assertIn("github.com", urls.get("Mirror", ""))
        self.assertIn("github.com", urls.get("Releases", ""))


if __name__ == "__main__":
    unittest.main()
