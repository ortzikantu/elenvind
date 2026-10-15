"""WSGI 入口：任何 WSGI 服务器都指向这里。

    gunicorn --bind 127.0.0.1:6789 elenvind.wsgi:application
    waitress-serve --listen=127.0.0.1:6789 elenvind.wsgi:application

导入即完成启动，顺序是硬性的：

    startup(STARTUP_HOOKS)   配置 / 日志 / i18n / 模板 / 建库与迁移 / 缓存预热
    application              暴露 WSGI callable（`elenvind.core.app.App` 实例）
    atexit shutdown()        退出前 flush 并关闭日志 handler

把初始化放在**导入时**，是因为 WSGI 只给了"导入模块"与"调用 callable"两个时机，
没有独立的启动/关闭协议。多进程服务器会在每个 worker 里各导入一次，所以
`startup()` 必须对"被执行多次"安全：

- 每一步都幂等（重读配置、重装日志、`CREATE TABLE IF NOT EXISTS`、按 `user_version`
  迁移、缓存预热）；
- 任何一步失败都抛异常 => 该 worker 导入失败 => 服务器拒绝启动，不会留下
  "看起来在跑、每个请求都出错"的进程。

启动钩子由装配层（`elenvind/app.py`）提供：Core 只提供 `startup(hooks)` 机制，
不知道有哪些业务模块。
"""
from __future__ import annotations

import atexit

from .app import STARTUP_HOOKS, app, prepare_database
from .core.lifespan import shutdown, startup

startup(STARTUP_HOOKS, prepare_database=prepare_database)
atexit.register(shutdown)

#: WSGI callable（PEP 3333）
application = app

__all__ = ["app", "application"]
