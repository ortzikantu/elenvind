"""选择并拉起一个 WSGI 服务器。

本应用是纯 WSGI callable（`elenvind.wsgi:application`），不绑定任何服务器实现。
这个模块负责三件事：探测当前环境有哪些服务器可用、按用户的显式选择或默认优先级
挑一个、用那个服务器自己的接口把应用拉起来。

服务器全部走 `pip install elenvind[<extra>]`，核心依赖里一个服务器都没有 —— 这样
"换服务器"永远只是装一个别的包，而不是改代码或改配置语义。
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .core.console import error, info, warning
from .core.utils import detect_physical_cores

#: 应用入口（WSGI callable 的导入路径）
APP_TARGET = "elenvind.wsgi:application"

#: `[server].workers = 0` 时按物理核心自适应，但不超过这个上限：
#: 同步 worker 各自要加载模板与内容缓存，而写事务本来就被 flock 串行化。
MAX_AUTO_WORKERS = 8


def resolve_worker_count(cli_workers, server_config):
    """解析进程/线程数，返回 `(数量, 来源说明)`。

    优先级：`--workers N` > `[server].workers`（> 0）> 按物理核心自适应。
    """
    if cli_workers:
        return int(cli_workers), "--workers"
    configured = int(server_config.get("workers", 0) or 0)
    if configured > 0:
        return configured, "config.toml [server].workers"
    cores = detect_physical_cores()
    return max(1, min(cores, MAX_AUTO_WORKERS)), f"auto from {cores} physical core(s)"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):        # 依赖缺失或包元数据损坏
        return False


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


@dataclass(frozen=True)
class Server:
    """一个可选 WSGI 服务器：检测方式、启动方式、以及它适合什么场景。"""

    name: str
    title: str
    extra: str
    when: str
    detect: object
    launch: object

    def available(self) -> bool:
        return bool(self.detect())


# ---------------------------------------------------------------- 启动实现


def _bind_parts(bind: str):
    host, _, port = bind.rpartition(":")
    return host or "127.0.0.1", int(port)


def _launch_gunicorn(bind, workers, server_config, options) -> int:
    """用 Gunicorn 的编程接口启动（进程模型；worker 数与超时可调）。"""
    from gunicorn.app.base import BaseApplication

    from .core.config import set_effective_bind

    # 把**实际**监听地址交给应用层：公网监听告警必须看真实 bind，而不是配置意图
    set_effective_bind(bind)
    settings = {
        "bind": bind,
        "workers": workers,
        "worker_class": "sync",
        "preload_app": True if options.get("preload") else None,
        "reload": True if options.get("reload") else None,
        "accesslog": "-" if options.get("access_log") else None,
        "forwarded_allow_ips": _forwarded_allow_ips(server_config),
        "loglevel": "warning",
        "proc_name": "elenvind",
        "timeout": int(server_config.get("timeout", 30) or 30),
    }
    class GunicornApp(BaseApplication):
        def __init__(self, gunicorn_settings):
            self.settings = gunicorn_settings
            super().__init__()

        def load_config(self):
            for key, value in self.settings.items():
                if value is None:
                    continue
                if key not in self.cfg.settings:
                    raise ValueError(f"unknown gunicorn setting: {key}")
                self.cfg.set(key, value)

        def load(self):
            from .wsgi import application  # 导入即完成 startup()

            return application

    GunicornApp(settings).run()
    return 0


def _launch_waitress(bind, workers, server_config, options) -> int:
    """用 Waitress 启动（纯 Python 线程模型；`workers` 在这里是线程数）。"""
    from waitress import serve

    from .wsgi import application

    host, port = _bind_parts(bind)
    serve(application, host=host, port=port, threads=max(1, workers),
          ident="elenvind")
    return 0


def _launch_subprocess(command, label) -> int:
    """把命令行交给外部服务器进程（uWSGI / Hypercorn 这类以 CLI 驱动的实现）。"""
    info(f"Starting {label}: {' '.join(command)}")
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        error(f"{label} 启动失败：找不到可执行文件 {command[0]}")
        return 2


def _launch_uwsgi(bind, workers, server_config, options) -> int:
    host, port = _bind_parts(bind)
    command = [shutil.which("uwsgi"), "--http", f"{host}:{port}",
               "--module", APP_TARGET, "--master",
               "--processes", str(workers), "--need-app"]
    if options.get("reload"):
        command.append("--py-autoreload=1")
    return _launch_subprocess(command, "uWSGI")


def _launch_hypercorn(bind, workers, server_config, options) -> int:
    host, port = _bind_parts(bind)
    command = [sys.executable, "-m", "hypercorn", "--bind", f"{host}:{port}",
               "--workers", str(workers), APP_TARGET]
    if options.get("reload"):
        command.append("--reload")
    return _launch_subprocess(command, "Hypercorn")


def _launch_wsgiref(bind, workers, server_config, options) -> int:
    """标准库开发服务器：单进程、单线程，**仅**用于本机验证。"""
    from wsgiref.simple_server import make_server

    from .wsgi import application

    host, port = _bind_parts(bind)
    warning("正在使用标准库 wsgiref：单进程、单线程、无连接超时。"
            "它只能用于本机验证，绝不能对外提供服务。")
    with make_server(host, port, application) as httpd:
        httpd.serve_forever()
    return 0


#: 探测顺序 = 默认优先级（越靠前越推荐用于生产）
SERVERS = (
    Server("gunicorn", "Gunicorn", "gunicorn",
           "多进程 + 连接超时 + 转发头白名单；本项目 CI 与文档的默认选择。",
           lambda: _has_module("gunicorn"), _launch_gunicorn),
    Server("waitress", "Waitress", "waitress",
           "纯 Python、无需编译；线程模型，不想装 C 扩展时最省心。",
           lambda: _has_module("waitress"), _launch_waitress),
    Server("uwsgi", "uWSGI", "uwsgi",
           "功能最全、性能最好，但需要编译；以 CLI 方式驱动。",
           lambda: _has_binary("uwsgi"), _launch_uwsgi),
    Server("hypercorn", "Hypercorn", "hypercorn",
           "支持 HTTP/1.1 与 HTTP/2；以 CLI 方式驱动。",
           lambda: _has_module("hypercorn"), _launch_hypercorn),
    Server("wsgiref", "标准库 wsgiref", "—",
           "免安装的开发服务器：单进程单线程，**不要**用于对外服务。",
           lambda: True, _launch_wsgiref),
)


def _forwarded_allow_ips(server_config) -> str:
    """把 `[server].trusted_proxies` 转成 Gunicorn 的 `forwarded_allow_ips`。

    应用自己也会读同一份列表来做安全判定；这里只是让 Gunicorn 的日志与错误页
    也认得这些对端，两边永远指向同一份配置。
    """
    trusted = server_config.get("trusted_proxies", ["127.0.0.1", "::1"])
    if isinstance(trusted, str):
        trusted = [item.strip() for item in trusted.split(",") if item.strip()]
    return ",".join(str(item) for item in trusted) or ""


# ---------------------------------------------------------------- 对外接口


def detect() -> list[Server]:
    """当前环境里可用的服务器（保持默认优先级顺序）。"""
    return [server for server in SERVERS if server.available()]


def find(name: str):
    """按名字（大小写不敏感）取一个服务器定义。"""
    wanted = str(name or "").strip().lower()
    for server in SERVERS:
        if server.name == wanted:
            return server
    return None


def choose(requested: str | None, configured: str | None, interactive: bool):
    """选定服务器：`--server` > 配置 > 交互询问 > 优先级最高的可用项。

    返回 `(Server, 来源说明)`；一个都不可用时返回 `(None, 原因)`。
    """
    for candidate, source in ((requested, "--server"), (configured, "config.toml")):
        if not candidate:
            continue
        server = find(candidate)
        if server is None:
            return None, f"未知的 WSGI 服务器 {candidate!r}（可选：{_names()}）"
        if not server.available():
            return None, (f"{server.title} 不可用：请先 `pip install "
                          f"elenvind[{server.extra}]`")
        return server, source

    available = detect()
    if not available:
        return None, (f"没有检测到任何 WSGI 服务器。装一个即可：{_install_hint()}")
    if len(available) == 1 or not interactive:
        return available[0], "自动选择"
    return _ask(available), "用户选择"


def _ask(available):
    """让用户从检测结果里挑一个（回车取推荐项）。"""
    print("\n检测到以下 WSGI 服务器：\n")
    for index, server in enumerate(available, 1):
        mark = "（推荐）" if index == 1 else ""
        print(f"  {index}) {server.title} {mark}")
        print(f"     {server.when}")
    while True:
        try:
            answer = input(f"\n请选择 [1-{len(available)}]（回车 = 1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return available[0]
        if not answer:
            return available[0]
        if answer.isdigit() and 1 <= int(answer) <= len(available):
            return available[int(answer) - 1]
        print("  输入无效，请重试。")


def _names() -> str:
    return ", ".join(server.name for server in SERVERS)


def _install_hint() -> str:
    extras = " / ".join(f"elenvind[{server.extra}]" for server in SERVERS[:4])
    return extras


def launch(server, bind, workers_source, workers, server_config, options) -> int:
    """把选定的服务器拉起来（阻塞直到它退出）。"""
    info(f"WSGI server: {server.title} ({server.name}); "
         f"{workers} process(es)/thread(s) ({workers_source})")
    return server.launch(bind, workers, server_config, options)
