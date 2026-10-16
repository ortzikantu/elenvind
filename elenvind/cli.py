"""Elenvind 命令行入口。

    elenvind init      生成站点实例（配置 + 内容目录 + 运行期目录）
    elenvind serve     自动检测并选择 WSGI 服务器，把站点拉起来
    elenvind backup    在线备份数据库
    elenvind doctor    环境体检：实例根、配置、数据库、WSGI 服务器、反向代理

所有子命令都读同一份 `config.toml`（位置由实例根决定：`ELENVIND_ROOT` →
从当前目录向上找 `config.toml` → 当前目录）。交互式提问可以用 `--yes` 关掉，
便于脚本与非交互环境。

本命令行**不修改任何系统配置**：反向代理与 TLS 证书的配置由管理员按
`manual/HOSTING.md` 手写，这里最多只做只读探测与体检。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from . import launcher, reverse_proxy, scaffold
from .core.console import banner, error, info, success, warning
from .core.version import get_version

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

def main(argv=None) -> int:
    """CLI 入口；返回进程退出码。"""
    parser = _parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    try:
        return handler(args)
    except KeyboardInterrupt:
        print()
        warning("已中断")
        return 130


# ---------------------------------------------------------------- 参数定义


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elenvind",
        description="Elenvind —— 安全默认的个人网站服务器"
                    "（标准库 + WSGI + SQLite + Jinja2，SSR + Zero-JS）",
        epilog="站点只监听回环地址，对外服务由反向代理提供；"
               "反代与 TLS 证书的配置按 manual/HOSTING.md 手写。",
    )
    parser.add_argument("--version", action="version",
                        version=f"elenvind {get_version()}")
    commands = parser.add_subparsers(dest="command", metavar="<命令>")

    init = commands.add_parser("init", help="生成站点实例")
    init.add_argument("--dir", default=".", metavar="PATH", help="实例目录（默认当前目录）")
    init.add_argument("--domain", metavar="DOMAIN", help="站点域名，写进 site_url")
    init.add_argument("--title", metavar="TEXT", help="站名")
    init.add_argument("--force", action="store_true", help="覆盖已存在的 config.toml")
    init.set_defaults(handler=_cmd_init)

    serve = commands.add_parser(
        "serve", help="启动站点（自动检测并选择 WSGI 服务器）",
        description="服务器由 pip extra 提供：elenvind[gunicorn] / [waitress] / "
                    "[uwsgi] / [hypercorn]；一个都没装时会用标准库 wsgiref（仅供本机验证）。")
    serve.add_argument("--server", metavar="NAME",
                       help="指定 WSGI 服务器，跳过询问（" +
                            "/".join(item.name for item in launcher.SERVERS) + "）")
    serve.add_argument("--bind", metavar="HOST:PORT", help="监听地址（默认取 config.toml）")
    serve.add_argument("--workers", type=int, metavar="N",
                       help="进程/线程数（0 = 按物理核心自适应，上限 "
                            f"{launcher.MAX_AUTO_WORKERS}）")
    serve.add_argument("--reload", action="store_true", help="代码改动自动重载（开发用）")
    serve.add_argument("--preload", action="store_true", help="在 master 进程导入应用")
    serve.add_argument("--access-log", action="store_true", help="打开访问日志")
    serve.add_argument("-y", "--yes", action="store_true", help="不提问：全部采用默认值")
    serve.set_defaults(handler=_cmd_serve)

    backup = commands.add_parser("backup", help="在线备份数据库")
    backup.add_argument("--to", metavar="PATH", help="备份文件路径（默认 backups/ 下带时间戳）")
    backup.set_defaults(handler=_cmd_backup)

    doctor = commands.add_parser("doctor", help="环境体检")
    doctor.set_defaults(handler=_cmd_doctor)

    return parser


# ---------------------------------------------------------------- 公共步骤


def _load_config():
    """加载并校验配置；失败时打印可操作的原因并返回 `None`。"""
    from .core.config import (
        CONFIG_PATH,
        ROOT,
        ConfigError,
        apply_runtime_config,
        config,
        load_config,
        validate_config,
    )

    if not CONFIG_PATH.is_file():
        error(f"没有找到配置文件：{CONFIG_PATH}")
        info("先做这两件事之一：")
        info("  elenvind init --dir <站点目录> --domain <域名>")
        info("  或设置 ELENVIND_ROOT 指向已有站点目录")
        return None
    try:
        load_config()
        validate_config()
        apply_runtime_config()
    except ConfigError as problem:
        error(f"配置无效：{problem}")
        return None
    info(f"实例根目录：{ROOT}")
    return config


def _banner() -> None:
    banner("=================================")
    banner("")
    banner("  ┏┓┓      •   ┓")
    banner("  ┣ ┃┏┓┏┓┓┏┓┏┓┏┫")
    banner("  ┗┛┗┗ ┛┗┗┛┗┛┗┗┻")
    banner("  A Personal Website Server")
    banner(f"  {get_version()}")
    banner("  Elen síla lúmenn' omentielvo.")
    banner("")
    banner("=================================")


# ---------------------------------------------------------------- 子命令


def _cmd_init(args) -> int:
    target = Path(args.dir).expanduser().resolve()
    created, skipped = scaffold.scaffold(target, domain=args.domain, title=args.title,
                                         force=args.force)
    success(f"站点实例：{target}")
    for path in created:
        info(f"  创建 {_relative(path, target)}")
    for path in skipped:
        warning(f"  已存在，未改动 {_relative(path, target)}")
    info("下一步：")
    info(f"  1) 编辑 {_relative(target / 'config.toml', target)}"
         "（site_url / admin_user_id / [static] 的资源地址）")
    info("  2) elenvind serve")
    info("  3) 按 manual/HOSTING.md 配好反向代理与证书（本工具不修改系统配置）")
    return EXIT_OK


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) or "."
    except ValueError:
        return str(path)


def _cmd_serve(args) -> int:
    config = _load_config()
    if config is None:
        return EXIT_FAILED
    server_config = config.get("server", {}) or {}
    bind = args.bind or f"{server_config.get('host', '127.0.0.1')}:" \
                        f"{server_config.get('port', 6789)}"
    workers, source = launcher.resolve_worker_count(args.workers, server_config)
    server, reason = launcher.choose(args.server, server_config.get("wsgi_server"),
                                     interactive=not args.yes)
    if server is None:
        error(reason)
        return EXIT_FAILED
    _banner()
    info(f"Listening on http://{bind} (loopback only)")
    info(f"Trusted proxies: {_trusted(server_config) or '(none)'}")
    info("TLS is terminated by the reverse proxy in front of this service.")
    return launcher.launch(server, bind, f"{reason}, {source}", workers,
                           server_config, vars(args))


def _trusted(server_config) -> str:
    trusted = server_config.get("trusted_proxies", ["127.0.0.1", "::1"])
    if isinstance(trusted, str):
        trusted = [item.strip() for item in trusted.split(",") if item.strip()]
    return ", ".join(str(item) for item in trusted if str(item).strip())


def _cmd_backup(args) -> int:
    config = _load_config()
    if config is None:
        return EXIT_FAILED
    from . import database
    from .app import resolve_db_path
    from .core.config import ROOT

    source = resolve_db_path()
    if args.to:
        target = Path(args.to).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = ROOT / "backups" / f"elenvind-{stamp}.db"
    try:
        written = database.backup_to(target, db_path=source)
    except database.BackupError as problem:
        error(str(problem))
        return EXIT_FAILED
    success(f"备份完成：{written}")
    info("恢复方法：停服务 → 移走当前 sqlite.db（连同 -wal / -shm）→ 放入备份文件。")
    return EXIT_OK


def _cmd_doctor(args) -> int:
    from .core.config import ROOT, config

    problems: list[str] = []
    info(f"elenvind {get_version()}；Python {sys.version.split()[0]}")
    info(f"实例根目录：{ROOT}")

    config_ok = _load_config() is not None
    if not config_ok:
        problems.append("配置不可用（见上面的原因）")

    if config_ok:
        _report_instance(ROOT, config, problems)

    print()
    info("可用的 WSGI 服务器：")
    for server in launcher.SERVERS:
        if server.available():
            extra = "" if server.extra == "—" else f"（elenvind[{server.extra}]）"
            info(f"  ✓ {server.title}{extra}")
        else:
            warning(f"  ✗ {server.title}：pip install elenvind[{server.extra}]")
    if not any(server.available() and server.name != "wsgiref"
               for server in launcher.SERVERS):
        problems.append("没有生产级 WSGI 服务器：pip install elenvind[gunicorn]")

    print()
    info("检测到的反向代理：")
    found = reverse_proxy.detect()
    if not found:
        warning("  一个都没有。站点的对外服务需要一个反向代理"
                "（nginx / caddy / apache2 / haproxy 任选其一）。")
    for proxy in found:
        info(f"  ✓ {proxy.title}：{proxy.when}")
        info(f"     配置：{proxy.config_hint}（写法见 manual/HOSTING.md）")

    print()
    if problems:
        error("体检发现问题：")
        for problem in problems:
            error(f"  - {problem}")
        return EXIT_FAILED
    success("体检通过：可以 `elenvind serve` 启动。")
    return EXIT_OK


def _report_instance(root: Path, config, problems: list[str]) -> None:
    """实例目录、数据库、可写性（这些是启动后必然会被写的东西）。"""
    from .app import resolve_db_path

    database = resolve_db_path()
    if database.is_file():
        info(f"数据库：{database}")
    else:
        warning(f"数据库尚未创建：{database}（首次启动会建库并迁移）")
    if not database.parent.is_dir():
        problems.append(f"数据库目录不存在：{database.parent}")

    for label, path in (("数据目录", database.parent),
                        ("日志目录", root / "logs"),
                        ("备份目录", root / "backups")):
        if path.is_dir():
            if os.access(path, os.W_OK):
                info(f"{label}：{path}（可写）")
            else:
                problems.append(f"{label}不可写：{path}")
        else:
            problems.append(f"{label}不存在：{path}")

    for key in ("articles_dir", "custom_pages_dir"):
        value = config.get(key)
        if value is None:
            continue
        directory = Path(value)
        if not directory.is_absolute():
            directory = root / directory
        if directory.is_dir():
            info(f"{key}：{directory}")
        else:
            warning(f"{key} 不存在：{directory}（对应页面会 404，不是错误）")

    host = str((config.get("server") or {}).get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "::1", "localhost"):
        problems.append(
            f"[server].host = {host!r} 不是回环地址：端口会直接暴露，"
            "绕过反向代理的 TLS/HSTS 与连接级防护")
    if not str(config.get("site_url", "")).startswith("https://"):
        problems.append("[site_url] 不是 https://…：Secure Cookie 与 HSTS 会失效")
    trusted = (config.get("server") or {}).get("trusted_proxies") or []
    if isinstance(trusted, list) and not trusted:
        warning("trusted_proxies 为空：应用不会采信 X-Forwarded-For/-Proto。"
                "仅当代理是 TCP 转发器或没有代理时才应该这样。")


if __name__ == "__main__":                     # pragma: no cover - 手工入口
    raise SystemExit(main())
