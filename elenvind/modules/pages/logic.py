"""Pages 模块：自定义页面（custom/*.md）。

页面是**纯 Markdown 正文**（无文档头），文件名即路由：`about.md` -> `/about`。
路径安全：slug 先过白名单校验；页面内容是扫描目录得到的，URL 只做字典查表，
从不参与文件路径拼接。

依赖：只依赖 `core/`（含共享的内容格式原语 `core.content`）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ...core.config import ROOT, config, resolve_path
from ...core.content import ContentError, validate_slug
from ...core.markdown import render_markdown

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 512 * 1024

_pages_cache = None       # slug -> Markup
_page_titles = {}         # slug -> 页面标题（首个一级标题，缺失则回退 slug）
_file_stats = None
#: 目录状态检查的最小间隔（秒）：命中缓存的 `/<slug>` 兜底请求不该每次都
#: glob 整个 custom 目录（与 blog 模块同一策略）。
_SCAN_INTERVAL_SECONDS = 2.0
_last_scan_at = 0.0


def custom_pages_dir() -> Path:
    """自定义页面目录：config.toml 顶层 `custom_pages_dir`（
    默认 `custom/`。"""
    return resolve_path(config.get("custom_pages_dir"), ROOT / "custom")


def _get_file_stats(dir_path: Path):
    stats = {}
    if dir_path.exists():
        for path in dir_path.glob("*.md"):
            try:
                stat = path.stat()
                stats[path.name] = (stat.st_mtime, stat.st_size)
            except OSError:
                continue
    return stats


def _first_heading(source: str) -> str:
    """页面**首个一级标题**的文本（`# About` -> `About`）；没有则返回空串。

    只认行首 `# `，且只看开头那一段（遇到正文就停）：自定义页面没有文档头，
    否则浏览器标签页只能显示文件名（`about - 站点名`）。
    """
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if title:
                return title
        break
    return ""


def _load_pages(dir_path: Path, stats: dict):
    """扫描目录渲染全部页面并抽取标题；单文件失败不影响其它页面，只记日志。"""
    pages = {}
    titles = {}
    if not dir_path.exists():
        return pages, titles
    for path in dir_path.glob("*.md"):
        try:
            if path.stat().st_size > MAX_PAGE_SIZE:
                raise ContentError(f"page too large: {path.name}")
            source = path.read_text(encoding="utf-8")
            pages[path.stem] = render_markdown(source)
            titles[path.stem] = _first_heading(source) or path.stem
        except (ContentError, OSError, UnicodeDecodeError, ValueError) as exc:
            logger.error("Failed to load page %s: %s", path.name, exc)
    return pages, titles


def _rescan() -> None:
    """一次性提交快照、页面缓存与标题（失败时都保持不变）。"""
    global _pages_cache, _page_titles, _file_stats, _last_scan_at
    directory = custom_pages_dir()
    new_stats = _get_file_stats(directory)
    new_pages, new_titles = _load_pages(directory, new_stats)
    _file_stats = new_stats
    _pages_cache = new_pages
    _page_titles = new_titles
    _last_scan_at = time.monotonic()


def _has_changes() -> bool:
    """目录是否需要重扫（按 `_SCAN_INTERVAL_SECONDS` 节流）。"""
    global _last_scan_at
    if _file_stats is None:
        return True
    now = time.monotonic()
    if now - _last_scan_at < _SCAN_INTERVAL_SECONDS:
        return False
    _last_scan_at = now
    return _get_file_stats(custom_pages_dir()) != _file_stats


def get_page(slug: str):
    """返回页面 Markup；slug 非法或页面不存在返回 None。"""
    if not validate_slug(slug):
        return None
    if _pages_cache is None or _has_changes():
        _rescan()
    return _pages_cache.get(slug)


def page_title(slug: str) -> str:
    """页面标题（首个一级标题；没有则回退 slug）。"""
    if _pages_cache is None or _has_changes():
        _rescan()
    return _page_titles.get(slug) or slug


def load_pages():
    """启动钩子：强制重扫。"""
    _rescan()


def page_slugs():
    """`/sitemap.xml` 的公开数据源：自定义页面的 slug 列表（页面无日期）。

    与 `get_page()` 共用同一份缓存与节流；由装配层注入给 seo 模块，
    保持"模块之间零 import"。
    """
    if _pages_cache is None or _has_changes():
        _rescan()
    return sorted(_pages_cache)
