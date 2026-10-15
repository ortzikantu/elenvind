"""版本号：唯一来源是 `pyproject.toml` 的 `[project].version`。

`raw_version()` 给出 PEP 440 版本号（`1.0.0`），`get_version()` 给出对外展示形式
（`v1.0.0`，页脚与 `<meta name="generator">` 用它）。

读取顺序：**源码检出**（含 `pip install -e .`）以本项目的 `pyproject.toml` 为准 ——
这样改了 `version` 立刻生效，不必先重装；否则问 `importlib.metadata`（wheel 里没有
pyproject 可读）。两者都拿不到就回落 `0.0.0`：版本号只是展示信息，宁可显示得难看，
也不该因为它把站点带崩。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

#: 分发包名，与 `pyproject.toml` 的 `[project].name` 一致
DISTRIBUTION = "elenvind"

_cache: str | None = None


def _installed_version() -> str:
    """已安装分发包的版本；未安装或元数据损坏时返回空串。"""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return str(version(DISTRIBUTION) or "").strip()
    except PackageNotFoundError:
        return ""
    except Exception:                      # 元数据损坏不该影响站点启动
        return ""


def _source_version(start: Path | None = None) -> str:
    """源码检出：从本文件向上找到最近一个**本项目**的 pyproject 的版本号。

    只认 `[project].name == "elenvind"` 的那个 pyproject：把虚拟环境放在别的项目
    目录里时（那种目录树里也有 pyproject.toml），不会误取别人的版本号。
    """
    base = (start or Path(__file__).resolve().parent).resolve()
    for candidate in (base, *base.parents):
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            with pyproject.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        project = data.get("project") or {}
        if str(project.get("name") or "").strip().lower() != DISTRIBUTION:
            continue
        value = str(project.get("version") or "").strip()
        if value:
            return value
    return ""


def raw_version() -> str:
    """PEP 440 版本号（如 `1.0.0`）。结果在进程内缓存。"""
    global _cache
    if _cache is None:
        _cache = _source_version() or _installed_version() or "0.0.0"
    return _cache


def get_version() -> str:
    """对外展示的版本号（如 `v1.0.0`）。"""
    return f"v{raw_version()}"


def reset_cache() -> None:
    """丢弃缓存（测试在切换安装状态时用）。"""
    global _cache
    _cache = None
