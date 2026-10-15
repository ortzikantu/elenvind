"""Elenvind 命令行入口。

    elenvind init      生成站点实例（配置 + 内容目录 + 运行期目录）
    elenvind serve     自动检测并选择 WSGI 服务器，把站点拉起来
    elenvind backup    在线备份数据库
    elenvind doctor    环境体检：实例根、配置、数据库、服务器、反代、证书
    elenvind proxy     探测反向代理并生成/写入它的站点配置

所有子命令都读同一份 `config.toml`（位置由实例根决定：`ELENVIND_ROOT` →
从当前目录向上找 `config.toml` → 当前目录）。危险操作（写系统路径、reload 服务）
一律要求显式 `--apply`；交互式提问可以被 `--yes` 关掉，便于脚本与非交互环境。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import launcher, reverse_proxy, scaffold
from .core.console import banner, error, info, success, warning
from .core.version import get_version

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

#: 上游地址里不能出现通配地址：代理要连的是"本机某端口"
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]", "*", ""}


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
        epilog="站点只监听回环地址，对外服务由反向代理提供："
               "`elenvind proxy configure --domain <域名> --apply`。",
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

    proxy = commands.add_parser("proxy", help="探测反向代理并生成/写入站点配置")
    proxy_commands = proxy.add_subparsers(dest="proxy_command", metavar="<动作>")
    detect = proxy_commands.add_parser("detect", help="列出检测到的反向代理")
    detect.add_argument("--domain", metavar="DOMAIN",
                        help="同时按域名报告：已有配置在哪个文件、是不是 elenvind 写的")
    detect.set_defaults(handler=_cmd_proxy_detect)
    certs = proxy_commands.add_parser("certs", help="按域名检索证书位置")
    certs.add_argument("--domain", metavar="DOMAIN", help="站点域名（默认取自 site_url）")
    certs.set_defaults(handler=_cmd_proxy_certs)
    configure = proxy_commands.add_parser(
        "configure",
        help="生成 443 + 80→443 配置（先按域名检索；只新增或就地更新受管段落，"
             "绝不覆盖别人的配置）")
    configure.add_argument("--proxy", metavar="NAME",
                           help="指定反向代理（" +
                                "/".join(item.name for item in reverse_proxy.PROXIES) + "）")
    configure.add_argument("--domain", metavar="DOMAIN", help="站点域名（默认取自 site_url）")
    configure.add_argument("--cert", metavar="PATH", help="证书链文件（默认找 Let's Encrypt）")
    configure.add_argument("--key", metavar="PATH", help="私钥文件")
    configure.add_argument("--apply", action="store_true",
                           help="写入并 reload（需要 root；改动已有文件前先留 .bak）")
    configure.add_argument("--with-unit", action="store_true",
                           help="同时安装 systemd 单元（需要 root）")
    configure.add_argument("--body-limit", metavar="SIZE", default=None,
                           help="代理侧请求体上限（默认 = 应用的 max_body_size）")
    configure.add_argument("-y", "--yes", action="store_true", help="不提问：全部采用默认值")
    configure.set_defaults(handler=_cmd_proxy_configure)
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


def _site_domain(config) -> str:
    """从 `site_url` 取域名；取不到就返回空串。"""
    return urlparse(str(config.get("site_url", "") or "")).hostname or ""


def _upstream(server_config) -> str:
    """代理要连的上游地址：通配监听一律收敛成回环地址。"""
    host = str(server_config.get("host", "127.0.0.1") or "").strip()
    port = int(server_config.get("port", 6789) or 6789)
    if host in _WILDCARD_HOSTS:
        host = "127.0.0.1"
    return f"{host}:{port}"


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
    info("  3) elenvind proxy configure --domain <域名> --apply")
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
        info(f"  ✓ {proxy.title}；配置会写到 {proxy.targets[0]}")

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


def _cmd_proxy_detect(args) -> int:
    found = reverse_proxy.detect()
    if not found:
        warning("没有检测到反向代理。装一个即可（nginx / caddy / apache2 / haproxy）。")
        return EXIT_OK
    domain = str(getattr(args, "domain", "") or "").strip()
    info("检测到以下反向代理：")
    for proxy in found:
        target = proxy.targets[0]
        marker = "存在" if target.is_dir() else "不存在"
        info(f"  {proxy.title}：配置目录 {target}（{marker}）")
        info(f"     {proxy.when}")
        if domain:
            _report_domain_files(proxy, domain)
    return EXIT_OK


def _report_domain_files(proxy, domain: str) -> None:
    """按域名报告已有配置：这是"新增还是就地更新"的唯一依据。"""
    hits = reverse_proxy.existing_domain_files(proxy, domain)
    if not hits:
        info(f"     {domain}：暂无配置 -> 会新增")
        return
    for path in hits:
        if reverse_proxy.is_ours(path, domain):
            info(f"     {domain}：{path} —— elenvind 受管段落，会**就地更新**这一段")
        else:
            warning(f"     {domain}：{path} —— 不是 elenvind 写的，configure 会拒绝改动")


def _cmd_proxy_certs(args) -> int:
    config = _load_config()
    if config is None:
        return EXIT_FAILED
    domain = (args.domain or _site_domain(config)).strip()
    if not domain:
        error("无法确定域名：用 --domain 指定，或把 config.toml 的 site_url 写成真实地址")
        return EXIT_USAGE
    from .core.config import ROOT

    info(f"按域名检索证书：{domain}（同时尝试 www 与裸域名的互换）")
    found = reverse_proxy.search_certificates(domain, ROOT)
    if found:
        for item in found:
            success(f"  找到：{item.cert} + {item.key}（来源：{item.source}）")
        info("configure 会使用列表里的第一个；也可用 --cert/--key 显式指定。")
        return EXIT_OK
    try:
        reverse_proxy.find_certificate(domain, instance_root=ROOT)
    except reverse_proxy.ProxyError as problem:
        error(str(problem))
    return EXIT_FAILED


def _cmd_proxy_configure(args) -> int:
    config = _load_config()
    if config is None:
        return EXIT_FAILED
    server_config = config.get("server", {}) or {}

    domain = (args.domain or _site_domain(config)).strip()
    if not domain:
        error("无法确定域名：用 --domain 指定，或把 config.toml 的 site_url 写成真实地址")
        return EXIT_USAGE

    proxy, reason = reverse_proxy.choose(args.proxy, interactive=not args.yes)
    if proxy is None:
        error(reason)
        return EXIT_FAILED

    from .core.config import ROOT

    try:
        certificate = reverse_proxy.find_certificate(domain, args.cert, args.key, ROOT)
    except reverse_proxy.ProxyError as problem:
        error(str(problem))
        return EXIT_FAILED

    spec = reverse_proxy.Render(
        domain=domain,
        upstream=_upstream(server_config),
        certificate=certificate,
        body_limit=args.body_limit or _body_limit(config),
        body_limit_bytes=_body_limit_bytes(config),
        read_timeout=reverse_proxy.read_source_timeout(server_config),
    )
    plan_ = reverse_proxy.plan(proxy, spec)

    print()
    info(f"反向代理：{proxy.title}（{reason}）")
    info(f"站点：{domain} -> http://{spec.upstream}")
    info(f"证书：{certificate.cert}（来源：{certificate.source}）")

    if plan_.blocked:
        error(plan_.message)
        return EXIT_FAILED

    info(f"动作：{plan_.action}")
    for detail in plan_.details:
        info(f"  - {detail}")
    print()
    for item in plan_.writes:
        print(f"----- {item.path} -----")
        print(item.text)

    if not args.apply:
        info("（dry-run，未改动任何文件）应用改动：sudo elenvind proxy configure "
             f"--proxy {proxy.name} --domain {domain} --apply")
        return EXIT_OK

    if os.geteuid() != 0:
        error("--apply 需要 root：请用 sudo 重新执行")
        return EXIT_FAILED
    try:
        written = reverse_proxy.apply(plan_)
    except reverse_proxy.ProxyError as problem:
        error(str(problem))
        return EXIT_FAILED
    for path in written:
        success(f"已写入 {path}（改动前的内容留在同名 .bak）")
    if proxy.extra_step:
        info(f"提示：{proxy.extra_step}")
    if not reverse_proxy.verify_and_reload(proxy):
        return EXIT_FAILED
    if args.with_unit:
        return _install_unit()
    return EXIT_OK


if __name__ == "__main__":                     # pragma: no cover - 手工入口
    raise SystemExit(main())


def _body_limit(config) -> str:
    """把应用的字节上限折算成代理配置里好读的形式（如 `2m`）。"""
    size = _body_limit_bytes(config)
    if size % (1024 * 1024) == 0:
        return f"{size // (1024 * 1024)}m"
    if size % 1024 == 0:
        return f"{size // 1024}k"
    return str(size)


def _body_limit_bytes(config) -> int:
    """代理侧请求体上限：比应用上限**大一点**，否则应用自己的 413 永远看不到。"""
    try:
        limit = int(config.get("max_body_size", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    return max(limit, 1024) + 1024


def _install_unit() -> int:
    """把包内的 systemd 单元装到系统上（需要 root）。"""
    import shutil

    source = scaffold.RESOURCES / "elenvind.service"
    target = Path("/etc/systemd/system/elenvind.service")
    if not source.is_file():
        error(f"包内缺少单元文件：{source}")
        return EXIT_FAILED
    if target.exists():
        warning(f"{target} 已存在，未覆盖；如需更新请手动对比 {source}")
    else:
        try:
            shutil.copyfile(source, target)
        except OSError as problem:
            error(f"无法写入 {target}：{problem}")
            return EXIT_FAILED
        success(f"已安装 {target}")
    for command in (("systemctl", "daemon-reload"),
                    ("systemctl", "enable", "--now", "elenvind")):
        ok, output = reverse_proxy.run(command)
        if not ok:
            error(f"{' '.join(command)} 失败：{output}")
            return EXIT_FAILED
    success("elenvind.service 已启用并启动")
    return EXIT_OK
