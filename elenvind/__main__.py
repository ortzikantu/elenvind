"""`python -m elenvind` 的入口：等价于 console script `elenvind`。

装过包就有 `elenvind` 命令；这个文件让"没装进 PATH / 用虚拟环境绝对路径"的场景也能
直接跑起来：`/srv/elenvind/.venv/bin/python -m elenvind serve`。
"""
from __future__ import annotations

from .cli import main

raise SystemExit(main())
