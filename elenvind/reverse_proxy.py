"""探测反向代理，生成或**就地更新**它的站点配置。

应用只监听回环地址，对外服务由反向代理提供。这里做的事只有三样：443 上转发到应用、
80 上永久跳转到 https、以及连接级超时。

三条不可协商的规则：

1. **先按域名检索**。写入前会在该代理的全部配置位置里找"是否已经有这个域名的配置"。
2. **绝不覆盖别人的配置**。已经有配置时：若是我们写的那一段（带标记）就**只替换那一段**；
   若没有标记（管理员自己写的），就**拒绝**并说明原因 —— 两个 `server_name` 相同的块
   会让代理冲突或按加载顺序选择，静默覆盖是最坏的结果。
3. **证书同样按域名检索**，找不到就报错并给出生成方法，而不是编一份指向不存在文件的配置。

默认 **dry-run**：只打印将要写入的内容；`--apply` 才落盘（改动已有文件前先留 `.bak`）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core.console import error, info, warning

#: 反代到应用的超时必须大于应用的 `[server].timeout`，
#: 否则应用还在处理连接就被代理切断（表现为随机的 502）。
DEFAULT_PROXY_READ_TIMEOUT = 40

#: 受管段落的标记。四种代理的注释符都是 `#`，因此同一套标记通用。
MARKER_BEGIN = "# >>> elenvind:{domain} >>>"
MARKER_END = "# <<< elenvind:{domain} <<<"

#: 认定"这段是我们写的"：新标记，或旧版本生成时写的头部注释
_OURS = re.compile(r"# >>> elenvind:|# Elenvind：")


class ProxyError(RuntimeError):
    """可预期的失败：路径不存在、权限不足、证书缺失、已有他人配置。"""


@dataclass(frozen=True)
class Certificate:
    cert: Path
    key: Path
    source: str = ""


@dataclass(frozen=True)
class Render:
    """渲染一次配置需要的全部输入。"""

    domain: str
    upstream: str
    certificate: Certificate
    body_limit: str = "2m"
    body_limit_bytes: int = 2 * 1024 * 1024
    read_timeout: int = DEFAULT_PROXY_READ_TIMEOUT


@dataclass(frozen=True)
class Write:
    """一次文件写入。`mode` 决定语义：`create` 只新建、`replace` 覆盖、
    `insert-line` 在文件末尾追加一行（用于 `include`/`import`）。"""

    path: Path
    text: str
    mode: str = "replace"


@dataclass(frozen=True)
class Plan:
    """一次配置变更的方案：可能包含多个文件的写入，或一个冲突。"""

    action: str
    writes: tuple[Write, ...] = ()
    conflict: Path | None = None
    message: str = ""
    details: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return self.conflict is not None


# ---------------------------------------------------------------- 代理定义


def _has_binary(*names: str) -> bool:
    return any(shutil.which(name) for name in names)


def _has_directory(path: Path) -> bool:
    return path.is_dir()


def _render_nginx(spec: Render) -> str:
    return f"""\
# Elenvind：{spec.domain}
# 只有三件事：80 -> 443、443 -> 应用、以及连接级超时。

limit_conn_zone $binary_remote_addr zone=elenvind_conn:10m;

server {{
    listen 80;
    listen [::]:80;
    server_name {spec.domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name {spec.domain};

    ssl_certificate     {spec.certificate.cert};
    ssl_certificate_key {spec.certificate.key};
    ssl_protocols       TLSv1.2 TLSv1.3;
    server_tokens off;

    client_header_timeout 10s;
    client_body_timeout   10s;
    send_timeout          10s;
    keepalive_timeout     30s;
    client_max_body_size  {spec.body_limit};

    location / {{
        limit_conn elenvind_conn 20;
        proxy_pass http://{spec.upstream};
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";
        proxy_connect_timeout 5s;
        proxy_read_timeout    {spec.read_timeout}s;
        proxy_send_timeout    {spec.read_timeout}s;
    }}

    location = /healthz {{
        access_log off;
        proxy_pass http://{spec.upstream}/robots.txt;
    }}
}}
"""


def _render_caddy(spec: Render) -> str:
    return f"""\
# Elenvind：{spec.domain}
# Caddy 自己也会申请证书；这里显式给出证书路径，等于固定成"已有证书"模式。
{spec.domain} {{
	tls {spec.certificate.cert} {spec.certificate.key}

	request_body {{
		max_size {spec.body_limit}
	}}

	reverse_proxy {spec.upstream} {{
		transport http {{
			dial_timeout  5s
			read_timeout  {spec.read_timeout}s
			write_timeout {spec.read_timeout}s
		}}
	}}
}}
"""


def _render_apache(spec: Render) -> str:
    return f"""\
# Elenvind：{spec.domain}
# 需要模块：ssl proxy proxy_http headers reqtimeout

<VirtualHost *:80>
    ServerName {spec.domain}
    Redirect permanent / https://{spec.domain}/
</VirtualHost>

<VirtualHost *:443>
    ServerName {spec.domain}

    SSLEngine on
    SSLCertificateFile    {spec.certificate.cert}
    SSLCertificateKeyFile {spec.certificate.key}
    SSLProtocol -all +TLSv1.2 +TLSv1.3

    Timeout 30
    LimitRequestBody {spec.body_limit_bytes}

    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass        / http://{spec.upstream}/ connectiontimeout=5 timeout={spec.read_timeout}
    ProxyPassReverse / http://{spec.upstream}/
</VirtualHost>
"""


def _haproxy_pem(spec: Render) -> str:
    """HAProxy 要求证书与私钥拼在同一个 PEM 文件里，这里给出两个路径。"""
    return f"{spec.certificate.cert} {spec.certificate.key}"


def _haproxy_name(spec: Render) -> str:
    return f"elenvind_{re.sub(r'[^A-Za-z0-9_]', '_', spec.domain)}"


def _render_haproxy(spec: Render) -> str:
    name = _haproxy_name(spec)
    return f"""\
# Elenvind：{spec.domain}

frontend {name}_http
    bind :80
    http-request redirect scheme https code 301

frontend {name}_https
    bind :443 ssl crt {_haproxy_pem(spec)}
    http-request set-header X-Forwarded-Proto https
    option forwardfor
    timeout client 30s
    default_backend {name}

backend {name}
    option httpchk GET /robots.txt
    server app {spec.upstream} check
    timeout connect 5s
    timeout server  {spec.read_timeout}s
"""


#: 反代定义表。`search` 是"按域名检索"的范围（目录或单个文件）；
#: `main_config` + `include_line` 只给单文件配置的代理（Caddy / HAProxy）用。
_PROXY_TABLES = (
    {
        "name": "nginx",
        "title": "nginx",
        "when": "最通用：发行版都打包了它，配置文件一放即生效。",
        "detect": lambda: _has_binary("nginx") or _has_directory(Path("/etc/nginx")),
        "targets": (Path("/etc/nginx/conf.d"), Path("/etc/nginx/sites-available"),
                    Path("/usr/local/etc/nginx/conf.d")),
        "filename": lambda domain: f"elenvind-{domain}.conf",
        "search": (Path("/etc/nginx/conf.d"), Path("/etc/nginx/sites-enabled"),
                   Path("/etc/nginx/sites-available"), Path("/etc/nginx/nginx.conf"),
                   Path("/usr/local/etc/nginx")),
        "main_config": None,
        "include_line": "",
        "render": _render_nginx,
        "test": ("nginx", "-t"),
        "reload": ("systemctl", "reload", "nginx"),
        "extra_step": "",
    },
    {
        "name": "caddy",
        "title": "Caddy",
        "when": "配置最短、证书全自动；把证书交给它管理时不用写 tls 行。",
        "detect": lambda: _has_binary("caddy") or _has_directory(Path("/etc/caddy")),
        "targets": (Path("/etc/caddy/Caddyfile.d"), Path("/etc/caddy")),
        "filename": lambda domain: f"{domain}.caddy",
        "search": (Path("/etc/caddy/Caddyfile"), Path("/etc/caddy/Caddyfile.d")),
        "main_config": Path("/etc/caddy/Caddyfile"),
        "include_line": "import Caddyfile.d/*",
        "render": _render_caddy,
        "test": ("caddy", "validate", "--config", "/etc/caddy/Caddyfile"),
        "reload": ("systemctl", "reload", "caddy"),
        "extra_step": "主配置里需要一行：import Caddyfile.d/*（本命令会自动补上）",
    },
    {
        "name": "apache",
        "title": "Apache httpd",
        "when": "已经在跑 Apache 时最省事；慢头/慢体有内置的 mod_reqtimeout。",
        "detect": lambda: _has_binary("apachectl", "apache2ctl", "httpd"),
        "targets": (Path("/etc/apache2/sites-available"), Path("/etc/httpd/conf.d"),
                    Path("/etc/apache2/conf.d")),
        "filename": lambda domain: f"elenvind-{domain}.conf",
        "search": (Path("/etc/apache2/sites-enabled"), Path("/etc/apache2/sites-available"),
                   Path("/etc/apache2/conf-enabled"), Path("/etc/apache2/conf.d"),
                   Path("/etc/httpd/conf.d"), Path("/etc/apache2/apache2.conf")),
        "main_config": None,
        "include_line": "",
        "render": _render_apache,
        "test": ("apachectl", "configtest"),
        "reload": ("systemctl", "reload", "apache2"),
        "extra_step": "Debian 系还需 a2enmod ssl proxy proxy_http headers reqtimeout "
                      "&& a2ensite elenvind-<域名>",
    },
    {
        "name": "haproxy",
        "title": "HAProxy",
        "when": "只做 TCP/HTTP 转发、追求稳定与吞吐；证书要拼成单个 PEM。",
        "detect": lambda: _has_binary("haproxy") or _has_directory(Path("/etc/haproxy")),
        "targets": (Path("/etc/haproxy/conf.d"), Path("/etc/haproxy")),
        "filename": lambda domain: f"elenvind-{domain}.cfg",
        "search": (Path("/etc/haproxy/haproxy.cfg"), Path("/etc/haproxy/conf.d")),
        "main_config": Path("/etc/haproxy/haproxy.cfg"),
        "include_line": "include /etc/haproxy/conf.d/*.cfg",
        "render": _render_haproxy,
        "test": ("haproxy", "-c", "-f", "/etc/haproxy/haproxy.cfg"),
        "reload": ("systemctl", "reload", "haproxy"),
        "extra_step": "主配置里需要一行：include /etc/haproxy/conf.d/*.cfg（本命令会自动补上）",
    },
)


@dataclass(frozen=True)
class ReverseProxy:
    """一个反向代理：探测、配置落点、按域名检索的范围、渲染与重载命令。"""

    name: str
    title: str
    when: str
    detect: object
    targets: tuple[Path, ...]
    filename: object
    render: object
    search: tuple[Path, ...]
    test: tuple[str, ...]
    reload: tuple[str, ...]
    extra_step: str = ""
    main_config: Path | None = None
    include_line: str = ""

    def available(self) -> bool:
        return bool(self.detect())

    def target(self, domain: str) -> Path:
        """新建配置时的落点：第一个**已存在**的目录，都不存在就用第一个候选。"""
        for directory in self.targets:
            if directory.is_dir():
                return directory / self.filename(domain)
        return self.targets[0] / self.filename(domain)


#: 对外的代理列表（`ReverseProxy` 实例）
PROXIES = tuple(ReverseProxy(**item) for item in _PROXY_TABLES)


def detect() -> list[ReverseProxy]:
    """当前系统上探测到的反向代理。"""
    return [proxy for proxy in PROXIES if proxy.available()]


def find(name: str):
    wanted = str(name or "").strip().lower()
    for proxy in PROXIES:
        if proxy.name == wanted:
            return proxy
    return None


def choose(requested: str | None, interactive: bool):
    """选定代理：`--proxy` > 唯一检出项 > 交互询问。"""
    if requested:
        proxy = find(requested)
        if proxy is None:
            return None, (f"未知的反向代理 {requested!r}（可选："
                          f"{', '.join(item.name for item in PROXIES)}）")
        return proxy, "--proxy"

    found = detect()
    if not found:
        return None, ("没有检测到反向代理。可以装 nginx / caddy / apache2 / haproxy "
                      "中的任意一个，再用 --proxy 指定。")
    if len(found) == 1 or not interactive:
        return found[0], "自动选择"
    return _ask(found), "用户选择"


def _ask(found):
    print("\n检测到以下反向代理：\n")
    for index, proxy in enumerate(found, 1):
        mark = "（推荐）" if index == 1 else ""
        print(f"  {index}) {proxy.title} {mark}")
        print(f"     {proxy.when}")
    while True:
        try:
            answer = input(f"\n请选择 [1-{len(found)}]（默认 1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return found[0]
        if not answer:
            return found[0]
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        print("  输入无效，请重试。")


# ---------------------------------------------------------------- 按域名检索


def _files_under(paths) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(item for item in sorted(path.rglob("*")) if item.is_file())
    return found


def domain_pattern(domain: str) -> re.Pattern:
    """域名匹配：两侧不能是域名可用的字符，避免 `example.com` 命中 `notexample.com.cn`。"""
    return re.compile(rf"(?<![\w.-]){re.escape(domain)}(?![\w.-])")


def existing_domain_files(proxy: ReverseProxy, domain: str) -> list[Path]:
    """在该代理的配置范围里找出所有提到这个域名的文件。"""
    pattern = domain_pattern(domain)
    hits = []
    for file in _files_under(proxy.search):
        try:
            if file.stat().st_size > 1_000_000:            # 配置文件不该这么大
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            hits.append(file)
    return hits


def is_ours(path: Path, domain: str) -> bool:
    """该文件里是否有我们的受管标记（或旧版本写的头部注释）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_OURS.search(text)) and domain_pattern(domain).search(text)


def managed_block(proxy: ReverseProxy, spec: Render) -> str:
    """带标记的受管段落：更新时只替换这一段，文件其余内容一个字节都不动。"""
    body = proxy.render(spec).rstrip("\n")
    return (f"{MARKER_BEGIN.format(domain=spec.domain)}\n"
            f"# 本段由 `elenvind proxy configure` 维护：重新运行只会更新这一段，\n"
            f"# 不会改动文件里的其它内容。需要接管请先删掉这两行标记。\n"
            f"{body}\n"
            f"{MARKER_END.format(domain=spec.domain)}\n")


def replace_managed_block(text: str, domain: str, block: str) -> str:
    """把文本里的受管段落换成新的，其余部分保持原样。"""
    begin = MARKER_BEGIN.format(domain=domain)
    end = MARKER_END.format(domain=domain)
    start = text.find(begin)
    stop = text.find(end)
    if start < 0 or stop < start:
        raise ProxyError(f"文件里找不到 {domain} 的受管标记，无法安全更新")
    stop += len(end)
    while stop < len(text) and text[stop] == "\n":
        stop += 1
    head, tail = text[:start], text[stop:]
    if head and not head.endswith("\n"):
        head += "\n"
    return head + block + tail


def plan(proxy: ReverseProxy, spec: Render) -> Plan:
    """决定"新建 / 就地更新 / 拒绝"三选一，不碰文件系统。"""
    domain = spec.domain
    hits = existing_domain_files(proxy, domain)
    ours = [path for path in hits if is_ours(path, domain)]
    theirs = [path for path in hits if path not in ours]

    if theirs:
        return Plan(
            action="冲突",
            conflict=theirs[0],
            message=(f"{theirs[0]} 里已经有 {domain} 的配置，而且不是 elenvind 写入的"
                     f"受管段落。\n"
                     f"  为避免两份 server_name 相同的配置互相冲突，本命令**不会**改动它。\n"
                     f"  请二选一：把手写的那份合并后删掉，或在确认可以替换后手动加入\n"
                     f"  `{MARKER_BEGIN.format(domain=domain)}` / "
                     f"`{MARKER_END.format(domain=domain)}` 两行标记再重跑。"))

    if ours:
        path = ours[0]
        try:
            text = path.read_text(encoding="utf-8")
            updated = replace_managed_block(text, domain, managed_block(proxy, spec))
        except (OSError, ProxyError) as problem:
            return Plan(action="冲突", conflict=path, message=str(problem))
        return Plan(action="就地更新受管段落", writes=(Write(path, updated),),
                    details=(f"只替换标记之间的内容：{path}",))

    block = managed_block(proxy, spec)
    target = proxy.target(domain)
    writes: list[Write] = []
    details: list[str] = []

    if proxy.main_config is not None and proxy.main_config.is_file():
        main = proxy.main_config
        try:
            main_text = main.read_text(encoding="utf-8")
        except OSError:
            main_text = ""
        if proxy.include_line and proxy.include_line not in main_text:
            writes.append(Write(main, main_text + _ensure_newline(main_text)
                                + f"# elenvind：纳入下方 drop-in 目录\n"
                                  f"{proxy.include_line}\n",
                                mode="insert-line"))
            details.append(f"追加一行 `{proxy.include_line}`：{main}")

    if target.exists():
        if is_ours(target, domain) or _OURS.search(_read(target)):
            writes.append(Write(target, block))
            details.append(f"更新 {target}")
        else:
            return Plan(action="冲突", conflict=target,
                        message=(f"{target} 已存在且不是 elenvind 写入的，本命令不会覆盖它。\n"
                                 f"  请先移走或改名，再重跑。"))
    else:
        writes.append(Write(target, block, mode="create"))
        details.append(f"新建 {target}")

    return Plan(action="新增配置", writes=tuple(writes), details=tuple(details))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _ensure_newline(text: str) -> str:
    if not text:
        return ""
    return "" if text.endswith("\n") else "\n"


# ---------------------------------------------------------------- 执行与重载


def apply(plan_: Plan) -> list[Path]:
    """落盘：改动已有文件前先留一份 `.bak`，新文件按原样写入。"""
    written: list[Path] = []
    for item in plan_.writes:
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            if item.path.exists() and item.mode != "create":
                backup = item.path.with_suffix(item.path.suffix + ".bak")
                shutil.copyfile(item.path, backup)
            temporary = item.path.with_suffix(item.path.suffix + ".new")
            temporary.write_text(item.text, encoding="utf-8")
            temporary.replace(item.path)
            written.append(item.path)
        except OSError as exc:
            raise ProxyError(f"无法写入 {item.path}：{exc}") from exc
    return written


def run(command: tuple[str, ...]) -> tuple[bool, str]:
    """执行校验 / reload 命令，返回 `(是否成功, 输出)`。"""
    if not command:
        return True, ""
    executable = shutil.which(command[0]) if not command[0].startswith("/") else command[0]
    if not executable:
        return False, f"找不到命令 {command[0]}"
    result = subprocess.run([executable, *command[1:]], capture_output=True,
                            text=True, check=False)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode == 0, output


def verify_and_reload(proxy: ReverseProxy) -> bool:
    """先校验配置，再 reload；任一步失败都保留现场并明确报错。"""
    if proxy.test:
        ok, output = run(proxy.test)
        if not ok:
            error(f"{proxy.title} 配置校验失败：\n{output}")
            return False
        info(f"{proxy.title} 配置校验通过")
    if not proxy.reload:
        warning(f"没有为 {proxy.title} 定义 reload 命令，请手动让它重载配置")
        return True
    ok, output = run(proxy.reload)
    if not ok:
        error(f"{proxy.title} reload 失败：{output}")
        return False
    info(f"{proxy.title} 已重载配置")
    return True


# ---------------------------------------------------------------- 证书检索


#: 证书检索顺序：先按域名在常见位置找，都找不到才报错
CERT_LOCATIONS = (
    ("Let's Encrypt（certbot）",
     "/etc/letsencrypt/live/{domain}/fullchain.pem",
     "/etc/letsencrypt/live/{domain}/privkey.pem"),
    ("acme.sh",
     "~/.acme.sh/{domain}/fullchain.cer",
     "~/.acme.sh/{domain}/{domain}.key"),
    ("/etc/ssl（Debian 风格）",
     "/etc/ssl/certs/{domain}.crt",
     "/etc/ssl/private/{domain}.key"),
    ("/etc/pki/tls（RHEL 风格）",
     "/etc/pki/tls/certs/{domain}.crt",
     "/etc/pki/tls/private/{domain}.key"),
    ("实例目录 tls/",
     "{instance}/tls/fullchain.pem",
     "{instance}/tls/privkey.pem"),
    ("实例目录 tls/（单文件命名）",
     "{instance}/tls/cert.pem",
     "{instance}/tls/key.pem"),
)


def certificate_candidates(domain: str, instance_root: Path | None = None) -> list[tuple]:
    """按域名列出**所有**候选位置（含 `www.` 与裸域名的互换）。"""
    root = str(instance_root or Path.cwd())
    names = [domain]
    if domain.startswith("www."):
        names.append(domain[4:])
    else:
        names.append(f"www.{domain}")
    candidates = []
    seen: set[tuple[Path, Path]] = set()
    for label, cert_tpl, key_tpl in CERT_LOCATIONS:
        for name in names:
            cert = Path(cert_tpl.format(domain=name, instance=root)).expanduser()
            key = Path(key_tpl.format(domain=name, instance=root)).expanduser()
            if (cert, key) in seen:            # 不看域名的模板会给出同一条，去重
                continue
            seen.add((cert, key))
            note = label if name == domain else f"{label}（{name}）"
            candidates.append((note, cert, key))
    return candidates


def search_certificates(domain: str, instance_root: Path | None = None) -> list[Certificate]:
    """按域名检索现有证书（存在且可读的）。"""
    found = []
    seen: set[tuple] = set()
    for label, cert, key in certificate_candidates(domain, instance_root):
        if (cert, key) in seen:
            continue
        seen.add((cert, key))
        if cert.is_file() and key.is_file():
            found.append(Certificate(cert, key, source=label))
    return found


def find_certificate(domain: str, cert: str | None = None, key: str | None = None,
                     instance_root: Path | None = None) -> Certificate:
    """定位证书：显式参数优先，其次按域名检索常见位置。

    两处都拿不到就抛 `ProxyError`，并在消息里给出生成方法与"重新运行即可"的提示 ——
    编一份指向不存在文件的 443 配置只会让代理启动失败或握手报错。
    """
    if cert or key:
        if not (cert and key):
            raise ProxyError("--cert 与 --key 必须同时给出")
        cert_path, key_path = Path(cert).expanduser(), Path(key).expanduser()
        for label, path in (("证书", cert_path), ("私钥", key_path)):
            if not path.is_file():
                raise ProxyError(f"{label}不存在：{path}")
        return Certificate(cert_path, key_path, source="--cert/--key")

    found = search_certificates(domain, instance_root)
    if found:
        return found[0]

    locations = "\n".join(
        f"  - {label}：{cert}" for label, cert, _key in certificate_candidates(domain,
                                                                             instance_root))
    raise ProxyError(
        f"没有找到 {domain} 的证书（已按域名检索下列位置）：\n{locations}\n"
        f"先生成一份，然后**重新运行本命令**即可（会自动检索并写入配置）：\n"
        f"  certbot certonly --nginx -d {domain}          # Let's Encrypt\n"
        f"  acme.sh --issue -d {domain} --nginx           # acme.sh\n"
        f"  openssl req -x509 -newkey rsa:2048 -nodes -days 90 -subj /CN={domain} \\\n"
        f"      -keyout key.pem -out cert.pem             # 自签，仅本地/测试\n"
        f"  或者显式指定：--cert <证书链> --key <私钥>")


def read_source_timeout(server_config) -> int:
    """代理侧读超时：始终比应用的 worker 超时大 10 秒，避开"边缘先断开"。"""
    return int(server_config.get("timeout", 30) or 30) + 10


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
