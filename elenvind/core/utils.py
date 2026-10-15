"""通用辅助：只依赖标准库的小工具（转义、规范化、IP 与请求元信息、CPU 探测）。"""
import contextlib
import html
import os
import pathlib
from datetime import date, datetime

from .config import config


def escape_html(text):
    """转义 HTML 特殊字符，防止 XSS"""
    return html.escape(str(text), quote=True)

def normalize_email(email):
    """规范化邮箱地址：去除首尾空白并转为小写（用于注册/登录/修改邮箱）"""
    return str(email).strip().lower()

def normalize_ip(value) -> str:
    """统一 IP 文本形式：去空白/方括号、小写、把 IPv4-mapped IPv6 还原成 IPv4。

    **为什么必须做**：`gunicorn --bind [::]` 这类双栈监听下，IPv4 客户端连接会以
    `::ffff:127.0.0.1` 形式出现在 `REMOTE_ADDR`。不归一化的话，默认的
    `trusted_proxies = ["127.0.0.1", "::1"]` 就匹配不上 —— 后果不是"少记一个 IP"，
    而是 `X-Forwarded-*` 全部不被采信：真实客户端 IP 丢失（IP 维度限流退化成
    "所有人共用一个桶"，一个攻击者就能把全站登录闸门拉满），scheme 退回 `http`
    （`cookie_prefix = true` 时浏览器会直接丢弃 `__Host-` Cookie，表现为登录不了）。
    """
    text = str(value or "").strip().strip("[]").lower()
    if not text:
        return ""
    if text.startswith("::ffff:") and "." in text[7:]:
        return text[7:]
    return text


def peer_address(environ) -> str:
    """直连对端地址（WSGI 的 `REMOTE_ADDR`，已归一化）；未知时返回 `"unknown"`。

    这是**应用唯一信任的**对端来源：它由 WSGI 服务器按真实 TCP 连接填入，
    客户端无法伪造（除非真的从那个地址连过来）。
    """
    if not environ:
        return "unknown"
    value = normalize_ip(environ.get("REMOTE_ADDR"))
    return value or "unknown"


def get_client_ip(headers, peer: str) -> str:
    """从请求头 + 直连对端解析客户端 IP 地址。

    安全边界：只有直连对端本身是受信代理（默认 127.0.0.1 / ::1，可用
    config.toml [server].trusted_proxies 覆盖）时才读取 `X-Forwarded-For`；
    否则一律使用直连 IP。防止应用端口被公网直连时伪造代理头绕过登录限流等
    IP 维度防护。

    **取最右侧的、非受信代理地址**（而不是最左侧）：X-Forwarded-For 的右侧
    是离应用最近的跳，由受信代理自己写入；左侧可能整段来自客户端提供的头。

    反例（本函数修掉的真实漏洞）：Nginx 用 `$proxy_add_x_forwarded_for` 时会把
    客户端自带的 `X-Forwarded-For` **原样保留在前面**，于是
    `X-Forwarded-For: 1.2.3.4, <真实客户端>` 里的 `1.2.3.4` 完全由攻击者选定。
    若取最左值，攻击者每请求换一个前缀就能绕过按 IP 计数的登录/注册/评论限流。
    从右往左跳过受信代理地址，可以在"覆盖"与"追加"两种代理配置下都拿到真实
    客户端（多级代理请把每一跳都写进 trusted_proxies）。
    """
    # 受信代理列表：默认回环地址；config 可配置（字符串或列表均可）
    peer = normalize_ip(peer)
    trusted = _trusted_proxies()
    if peer not in trusted:
        return peer or "unknown"

    forwarded = str(headers.get("x-forwarded-for", "") or "")
    hops = [normalize_ip(part) for part in forwarded.split(",") if part.strip()]
    if not hops:
        return peer
    for hop in reversed(hops):
        if hop not in trusted:
            return hop
    # 全是受信代理地址（代理链配置异常）：不伪造，退回最左值
    return hops[0]


def get_request_scheme(environ, headers) -> str:
    """请求 scheme：`"http"` 或 `"https"`。

    WSGI 服务器只报告**它自己看到的那条连接**：TLS 在反向代理上终止时应用收到的是
    明文，`wsgi.url_scheme` 永远是 `"http"`。判定顺序：

    1. 服务器报告 `https`（说明 WSGI 之前已有人终结 TLS）-> `https`；
    2. `site_url` **声明**为 `https` -> 一律 `https`，不看对端是否受信、也不看
       `X-Forwarded-Proto`。理由：代理可能"诚实地"说 `http`（CDN 到源站那一段常是
       明文），照单全收会让 `Secure` Cookie 与 HSTS 无声失效；而若这一步依赖"对端
       受信"，用最简 TCP 转发器（socat / DNAT，不覆写请求头）时客户端伪造的
       `X-Forwarded-Proto: https` 就会被当真。让管理员对外的唯一声明 `site_url`
       裁决，行为就不随代理实现漂移 —— 代价是刻意 fail-closed：声明 https 却在明文
       上访问应用端口时 Cookie 会被丢弃、登录不了（本地明文测试请把 `site_url`
       临时写成 `http://…`）；
    3. 只有 `site_url` 未声明 https 时，才看"受信对端 + `X-Forwarded-Proto`"，取
       **最右**一个有效值（最靠近应用的那一跳）；非受信对端说的话一律忽略。

    白名单与 `get_client_ip()` 共用同一份 `[server].trusted_proxies`：信任边界一旦有
    两份列表就会漂移，而漂移的后果正是 HTTPS 下 Secure Cookie 丢失。
    """
    scheme = ""
    if environ:
        scheme = str(environ.get("wsgi.url_scheme", "") or "").strip().lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    if scheme == "https" or _declared_site_scheme() == "https":
        return "https"                       # 见 docstring 的第 1、2 条
    if peer_address(environ) not in _trusted_proxies():
        return scheme
    forwarded = str(headers.get("x-forwarded-proto", "") or "")
    for value in reversed([part.strip().lower() for part in forwarded.split(",")]):
        if value == "https":
            return "https"
    return scheme


def _cgroup_cpu_quota(root: str = "/sys/fs/cgroup"):
    """cgroup 允许使用的 CPU 核数上限；没有限制 / 读不到时返回 None。

    容器里 `/proc/cpuinfo` 与 `os.cpu_count()` 报的是**宿主机**核数：一台 64 核
    机器上限制 2 核的容器会开出 8 个 worker（上限 8），白占内存还可能被 OOM。
    因此把 cgroup 配额作为**下界约束**参与取小。

    支持 cgroup v2（`cpu.max` = "quota period" 或 "max period"）与
    v1（`cpu/cpu.cfs_quota_us` + `cpu/cpu.cfs_period_us`，-1 表示不限）。
    """
    try:
        raw = pathlib.Path(root, "cpu.max").read_text(encoding="ascii").split()
        quota, period = raw[0], raw[1]
        if quota != "max":
            return max(-(-int(quota) // int(period)), 1)     # 向上取整
        return None
    except (OSError, IndexError, ValueError):
        pass
    try:
        quota = int(pathlib.Path(root, "cpu/cpu.cfs_quota_us").read_text(encoding="ascii"))
        period = int(pathlib.Path(root, "cpu/cpu.cfs_period_us").read_text(encoding="ascii"))
        if quota > 0 and period > 0:
            return max(-(-quota // period), 1)
    except (OSError, ValueError):
        pass
    return None


def detect_physical_cores() -> int:
    """可用 CPU 核数（优先**物理**核，并受 cgroup 配额与 CPU 亲和集约束）。

    为什么这么绕：同步 worker 各自要加载模板与内容缓存，开多了只会白占内存
    （写事务本来就被 flock 串行化）；而 `os.cpu_count()` 在超线程机器上会翻倍、
    在容器里又会报宿主机核数。因此取以下几者的**最小值**：

    1. cgroup v2/v1 的 CPU 配额（容器里真正的上限）；
    2. `/proc/cpuinfo` 的 (physical id, core id) 去重（超线程不加倍）；
    3. 本进程的 CPU 亲和集（cpuset 约束）；
    4. `os.cpu_count()`。

    一个都拿不到时返回 1（永不返回 0）。
    """
    candidates = []
    quota = _cgroup_cpu_quota()
    if quota:
        candidates.append(quota)
    physical = _physical_cores_from_proc()
    if physical:
        candidates.append(physical)
    with contextlib.suppress(AttributeError, OSError):   # pragma: no cover - 平台分支
        candidates.append(len(os.sched_getaffinity(0)))
    logical = os.cpu_count()
    if logical:
        candidates.append(logical)
    return max(min(candidates), 1) if candidates else 1


def _physical_cores_from_proc(proc_path: str = "/proc/cpuinfo"):
    """`/proc/cpuinfo` 里的物理核心数；读不到返回 None。"""
    try:
        cores: set[str] = set()
        sockets: set[str] = set()
        with open(proc_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("core id"):
                    cores.add(line.split(":", 1)[1].strip())
                elif line.startswith("physical id"):
                    sockets.add(line.split(":", 1)[1].strip())
        if cores:
            return max(len(cores) * max(len(sockets), 1), 1)
    except OSError:
        pass
    return None


def _declared_site_scheme() -> str:
    """`site_url` 里声明的对外协议（`"https"` / `"http"` / `""`）。

    只认配置里那一个绝对地址，绝不从请求的 Host / 头推导（防 Host 头投毒）。
    """
    value = str(config.get("site_url", "") or "").strip().lower()
    if value.startswith("https://"):
        return "https"
    if value.startswith("http://"):
        return "http"
    return ""


# 受信代理列表缓存：配置只在启动时加载一次，无需每个请求重新解析
_TRUSTED_CACHE = None
_TRUSTED_SOURCE = object()


def _trusted_proxies() -> tuple:
    """解析受信代理配置：config.toml [server].trusted_proxies，默认仅回环地址。

    结果按配置内容缓存：配置只在启动时加载一次，无需每个请求重新解析。
    """
    global _TRUSTED_CACHE, _TRUSTED_SOURCE
    configured = config.get("server", {}).get("trusted_proxies")
    if _TRUSTED_CACHE is not None and configured == _TRUSTED_SOURCE:
        return _TRUSTED_CACHE
    if configured is None:
        resolved = ("127.0.0.1", "::1")
    elif isinstance(configured, str):
        resolved = tuple(normalize_ip(ip) for ip in configured.split(",")
                         if normalize_ip(ip))
    else:
        resolved = tuple(normalize_ip(ip) for ip in configured if normalize_ip(ip))
    _TRUSTED_CACHE = resolved
    _TRUSTED_SOURCE = configured
    return resolved


def format_datetime(value):
    """
    将 ISO 字符串或 datetime 对象格式化为 'YYYY-MM-DD HH:MM'
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value  # 解析失败时原样返回（不属于程序缺陷）
    else:
        return str(value)
    return dt.strftime("%Y-%m-%d %H:%M")


def format_date(value):
    """
    将 ISO 字符串或 datetime 对象格式化为 'YYYY-MM-DD'
    """
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value).date()
        except ValueError:
            return value
    else:
        return str(value)
    # datetime 与 date 都有 strftime("%Y-%m-%d")，不需要分支
    return dt.strftime("%Y-%m-%d")
