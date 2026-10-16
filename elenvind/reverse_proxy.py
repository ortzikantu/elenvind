"""探测系统上装了哪个反向代理（**只读**）。

本服务**不改动**任何反向代理的配置：TLS、http→https 跳转与连接级慢连接由代理承担，
配置怎么写见 `manual/HOSTING.md`（nginx / Caddy / Apache / HAProxy 的等价写法 +
证书获取与续期）。这里只回答一个问题："这台机器上装了什么"，供 `elenvind doctor`
和管理员判断。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


def _has_binary(*names: str) -> bool:
    return any(shutil.which(name) for name in names)


def _has_directory(path: Path) -> bool:
    return path.is_dir()


@dataclass(frozen=True)
class ReverseProxy:
    """一个可选的反向代理：名字、适用场景、配置落点提示、探测方式。"""

    name: str
    title: str
    when: str
    config_hint: str
    detect: object

    def available(self) -> bool:
        return bool(self.detect())


#: 探测列表（顺序 = 文档里介绍的顺序）
PROXIES = (
    ReverseProxy(
        name="nginx",
        title="nginx",
        when="最通用：发行版都打包了它，最小配置就能转发 + 跳转。",
        config_hint="/etc/nginx/conf.d/ 或 /etc/nginx/sites-available/",
        detect=lambda: _has_binary("nginx") or _has_directory(Path("/etc/nginx")),
    ),
    ReverseProxy(
        name="caddy",
        title="Caddy",
        when="配置最短、证书自动申请与续期；不想手写 TLS 时最省心。",
        config_hint="/etc/caddy/Caddyfile",
        detect=lambda: _has_binary("caddy") or _has_directory(Path("/etc/caddy")),
    ),
    ReverseProxy(
        name="apache",
        title="Apache httpd",
        when="已经在跑 Apache 时最省事；慢头/慢体有内置的 mod_reqtimeout。",
        config_hint="/etc/apache2/sites-available/ 或 /etc/httpd/conf.d/",
        detect=lambda: _has_binary("apachectl", "apache2ctl", "httpd"),
    ),
    ReverseProxy(
        name="haproxy",
        title="HAProxy",
        when="只做 TCP/HTTP 转发、追求稳定与吞吐；证书要拼成单个 PEM。",
        config_hint="/etc/haproxy/haproxy.cfg",
        detect=lambda: _has_binary("haproxy") or _has_directory(Path("/etc/haproxy")),
    ),
)


def detect() -> list[ReverseProxy]:
    """当前系统上探测到的反向代理（保持 `PROXIES` 的顺序）。"""
    return [proxy for proxy in PROXIES if proxy.available()]
