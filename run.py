"""兼容入口：把旧的 `python run.py` 旗标翻译成 `elenvind` 子命令。

新代码请直接用 console script（`elenvind serve` / `elenvind backup` / …）或
`python -m elenvind.cli`。这个文件只保留一层参数翻译，让既有文档、systemd 单元与
脚本里的 `python run.py --bind 127.0.0.1:6789` 继续可用。

保留它的另一个原因：`tests/` 里仍按 `run.py` 的旧接口取 `MAX_AUTO_WORKERS` 与
`resolve_worker_count`，这里原样转发，避免测试与实现出现两份 worker 解析逻辑。
"""
from __future__ import annotations

import sys

from elenvind.cli import main as _cli_main
from elenvind.launcher import MAX_AUTO_WORKERS, resolve_worker_count

__all__ = ["MAX_AUTO_WORKERS", "main", "resolve_worker_count"]

#: 旧旗标 -> 新子命令；值型旗标前需要带上它自己
_SERVE_FLAGS = ("--workers", "--bind")
_SERVE_SWITCHES = ("--reload", "--preload", "--access-log")


def _translate(argv: list[str]) -> list[str]:
    """旧参数 -> `elenvind` 子命令参数。"""
    if not argv or argv[0] in ("-h", "--help"):
        return ["serve", "--help"] if argv else ["serve"]

    if "--backup" in argv or "--backup-to" in argv:
        translated = ["backup"]
        if "--backup-to" in argv:
            index = argv.index("--backup-to")
            if index + 1 >= len(argv):
                raise ValueError("--backup-to 需要一个取值")
            translated += ["--to", argv[index + 1]]
        unknown = [item for item in argv
                   if item not in ("--backup", "--backup-to")
                   and item not in translated]
        if unknown:
            raise ValueError(f"--backup 不能与这些参数同时使用：{' '.join(unknown)}")
        return translated

    translated = ["serve"]
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in _SERVE_FLAGS:
            if index + 1 >= len(argv):
                raise ValueError(f"{item} 需要一个取值")
            translated += [item, argv[index + 1]]
            index += 2
            continue
        if item in _SERVE_SWITCHES:
            translated.append(item)
            index += 1
            continue
        if item in ("-y", "--yes"):
            translated.append("--yes")
            index += 1
            continue
        raise ValueError(f"未知选项: {item}")
    return translated


def main(argv=None) -> int:
    """翻译参数后交给 CLI；翻译失败按用法错误处理（退出码 2）。"""
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        translated = _translate(raw)
    except ValueError as problem:
        from elenvind.core.console import error, warning

        error(str(problem))
        warning("等价的新用法：elenvind serve --help / elenvind backup --help")
        return 2
    return _cli_main(translated)


if __name__ == "__main__":
    sys.exit(main())
