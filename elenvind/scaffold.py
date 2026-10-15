"""`elenvind init`：把一个空目录变成可运行的站点实例。

生成的实例包含：`config.toml`（从包内示例复制，可选写入域名）、内容目录
（`articles/`、`custom/`）、运行期目录（`data/`、`logs/`、`backups/`），
以及一份挡住数据与日志的 `.gitignore`。

刻意只创建**缺失**的东西：已存在的文件一律不动，除非显式 `--force`。
"""
from __future__ import annotations

import re
from pathlib import Path

#: 包内资源目录（配置示例等随包分发的东西）
RESOURCES = Path(__file__).resolve().parent / "resources"

#: 实例骨架：内容目录与运行期目录
INSTANCE_DIRS = ("articles", "custom", "data", "logs", "backups")

_GITIGNORE = """\
# 站点实例的运行期产物：数据、日志、备份都不该进版本库
data/
logs/
backups/
*.db
*.db-*
*.write.lock
*.pre-v*.db
__pycache__/
*.py[cod]
.venv/
"""


def example_config() -> Path:
    """包内示例配置的路径（`elenvind init` 与文档共用这一份）。"""
    return RESOURCES / "config.example.toml"


def _render_config(domain: str | None, title: str | None) -> str:
    """读示例配置，只替换域名/站名这两行 —— 其余注释与默认值原样保留。"""
    text = example_config().read_text(encoding="utf-8")
    if domain:
        text = re.sub(r'^site_url = .*$', f'site_url = "https://{domain}"',
                      text, count=1, flags=re.M)
    if title:
        text = re.sub(r'^title = .*$', f'title = "{title}"', text, count=1, flags=re.M)
    return text


def scaffold(target: Path, *, domain: str | None = None, title: str | None = None,
             force: bool = False) -> tuple[list[Path], list[Path]]:
    """在 `target` 下生成实例骨架，返回 `(已创建, 已存在而跳过)`。"""
    target = Path(target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    skipped: list[Path] = []

    for name in INSTANCE_DIRS:
        directory = target / name
        if directory.is_dir():
            skipped.append(directory)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)

    config = target / "config.toml"
    if config.exists() and not force:
        skipped.append(config)
    else:
        config.write_text(_render_config(domain, title), encoding="utf-8")
        created.append(config)

    ignore = target / ".gitignore"
    if ignore.exists() and not force:
        skipped.append(ignore)
    else:
        ignore.write_text(_GITIGNORE, encoding="utf-8")
        created.append(ignore)

    return created, skipped
