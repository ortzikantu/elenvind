"""Elenvind Core：应用运行所必需的通用机制。

| 领域 | 模块 |
|---|---|
| 配置与运行环境 | `config`、`version`、`console` |
| WSGI 入口与请求边界 | `app`、`http` |
| 路由与错误页 | `routing` |
| 会话 / Cookie / CSRF / 认证授权 | `session`、`security`、`csrf`、`auth` |
| 模板与内容格式 | `templating`、`content`、`markdown`、`i18n`、`context` |
| 启动与关闭 | `lifespan`、`logging_config` |
| 通用辅助 | `utils` |

边界（由 `tests/test_architecture.py` 守卫）：

- **不认识 database，也不认识业务模块**，更不 import 装配层。数据库相关的一切在
  `elenvind/database/`；core 需要的数据源（会话 store、用户总数、数据库启动步骤）由装配层注入。
- **不持有"模块注册表"这类全局可变状态**：启动钩子由装配层通过
  `lifespan.startup(hooks)` 显式传入。
- 安全机制（会话 / CSRF / Cookie / 密码 / 安全响应头 / 请求限制）在这里实现一次；
  业务规则属于 `modules/`。
"""
