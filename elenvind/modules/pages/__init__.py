"""pages 模块：自定义页面（`custom/*.md`）与主题偏好入口。

公开入口：
- `logic`：扫描/缓存 `custom_pages_dir` 下的 Markdown，提供 `get_page(slug)`；
- `routes.register(router, render_not_found=...)`：`/theme` 与 `/<slug>` 兜底路由。

`/<slug>` 声明为 `fallback=True`，只在其它固定路径都没匹配时才接手，因此自定义页面
不会遮蔽 `/login`、`/article/...` 这类真实路由。依赖 `core`（内容格式原语在
`core.content`）与 `database`。
"""
