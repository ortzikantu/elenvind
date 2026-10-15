"""Auth 模块：登录、注册、登出。

密码 / 会话 / CSRF / Cookie 全部由 Core 负责：
- 校验密码 -> `core.auth.verify_credentials`（含透明 rehash）
- 颁发会话 -> `core.session.login_user`（含防会话固定轮换）
- CSRF      -> 调度器默认施加（本模块**不写**任何 CSRF 代码）
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from ... import database
from ...core.auth import verify_credentials
from ...core.config import config
from ...core.context import current_lang
from ...core.http import html, redirect, safe_next_path
from ...core.i18n import t
from ...core.security import (
    EMAIL_MAX,
    NICKNAME_MAX,
    PASSWORD_MAX,
    PASSWORD_MIN,
    dummy_verify,
    hash_password,
    is_weak_password,
)
from ...core.session import login_user, logout_user
from ...core.templating import render_template

# 捕获"邮箱已存在"要用的异常类型：由 Core 重新导出，模块不需要（也不该）
# import sqlite3 —— 见 Core Contract 守卫。
from ...database import IntegrityError
from ...database.auth import (
    complete_login_success,
    register_attempt_exceeded,
    reserve_login_attempt,
    try_register_attempt,
)
from ...database.user import create_user, get_user_by_email, normalize_email

logger = logging.getLogger(__name__)

#: 登录限流默认阈值（可被 [login_limits] 覆盖）
DEFAULT_LOGIN_LIMITS = {
    "max_email_failures": 5,
    "email_window_seconds": 24 * 3600,
    "max_ip_failures": 20,
    "ip_window_seconds": 15 * 60,
    "max_global_failures": 200,
    "global_window_seconds": 15 * 60,
}
#: 注册限流默认阈值
DEFAULT_REGISTER_LIMITS = {"max_per_ip": 5, "window_seconds": 3600}


def login_limits():
    return _limits("login_limits", DEFAULT_LOGIN_LIMITS)


def register_limits():
    return _limits("register_limits", DEFAULT_REGISTER_LIMITS)


def _limits(section, defaults):
    raw = config.get(section) or {}
    limits = dict(defaults)
    if isinstance(raw, dict):
        for key in limits:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
    return limits


def registration_enabled() -> bool:
    return bool(config.get("registration_enabled", True))


#: `?changed=` 的**白名单**：URL 只能在这些取值里选，绝不能让 URL 决定文案。
_NOTICE_KEYS = {
    "password": "user_ok_password_changed",
    "account_deleted": "user_ok_account_deleted",
}


def input_limits():
    """表单字段长度限制（模板与后端共用同一组值）。"""
    return {
        "email": EMAIL_MAX,
        "nickname": NICKNAME_MAX,
        "password": PASSWORD_MAX,
        "password_min": PASSWORD_MIN,
    }


def _message(lang, key):
    return t(lang, key)


def register(router):
    """把认证路由装到 router 上。

    模块只声明 `auth=` / `permission=`：认证闸门的 403 由 Core 的
    `router.dispatch(forbidden=...)` 统一处理。这里刻意不收 `render_forbidden`
    之类的回调 —— 一个收下却永远不用的参数会让人以为"权限被拒时会走这里"。
    """
    @router.route("/login", methods=["GET", "POST"])
    def login(request):
        lang = current_lang()
        message = ""
        message_kind = "error"
        # 回跳地址：来自认证闸门的 `?next=`，只接受站内路径（防开放重定向）
        next_path = safe_next(request.form.get("next") or request.arg("next"))
        if request.method == "POST":
            email = normalize_email(request.form.get("email", ""))
            password = request.form.get("password", "")
            message, retry_after = _try_login(request, email, password, lang)
            if message is None:
                # 会话 Cookie 与 CSRF Cookie 由 Core 的 send_response 统一下发
                # （模块不拼 Cookie 名，因此 __Host- 前缀之类的策略自动生效）
                return redirect(next_path or "/")
        else:
            retry_after = 0
            # 改密 / 删号后的一次性反馈（`?changed=` 白名单）
            notice = _NOTICE_KEYS.get(request.arg("changed", ""))
            if notice:
                message, message_kind = _message(lang, notice), "success"
        response = html(render_template("auth/login.html", {
            "message": message, "message_kind": message_kind,
            "limits": input_limits(),
            "next_path": next_path,
            "registration_enabled": registration_enabled(),
        }))
        if retry_after:
            # 被限流时明确告诉客户端还要等多久（不再是一句模糊的"稍后再试"）
            response.headers.append((b"Retry-After", str(int(retry_after)).encode("ascii")))
        # 页面上带 CSRF 令牌：绝不允许共享缓存留一份给别人复用（no-cache 允许
        # 304 复用旧正文，no-store 才是"别存"）。
        return response.with_cache_control("no-store")

    @router.route("/register", methods=["GET", "POST"])
    def register(request):
        lang = current_lang()
        message = ""
        next_path = safe_next(request.form.get("next") or request.arg("next"))
        enabled = registration_enabled()
        form_nickname = request.form.get("nickname", "")
        form_email = normalize_email(request.form.get("email", ""))
        if request.method == "POST" and enabled:
            result = _try_register(request, lang)
            if result is None:
                # 注册成功 -> 去登录，并把回跳地址继续带着
                return redirect(f"/login?next={quote(next_path, safe='')}"
                                if next_path else "/login")
            message = result
        elif not enabled:
            # 关闭注册时**直接说清楚**（GET 就说明），而不是让访客填完表单再被拒；
            # 同时不进入 _try_register —— 那里会先记一次注册尝试流水。
            message = _message(lang, "auth_err_register_disabled")
        return html(render_template("auth/register.html", {
            "message": message, "message_kind": "error",
            "limits": input_limits(),
            "next_path": next_path,
            "registration_enabled": enabled,
            "form_nickname": form_nickname,
            "form_email": form_email,
        })).with_cache_control("no-store")

    @router.route("/logout", methods=["GET"])
    def logout_confirm(request):
        """友好的退出确认页。

        **只读**：不在 GET 里销毁会话（那会让退出变成可被 `<img src>` 触发的
        状态变更）。真正的退出由下面的 POST + CSRF 完成。
        未登录时（或会话已失效）也走到这里，页面渲染"你已退出登录"。
        """
        return html(render_template("auth/logout.html", {})).with_cache_control("no-store")

    @router.route("/logout", methods=["POST"], auth="required")
    def logout(request):
        # 只调 Core：它负责删服务端会话**并**让浏览器 Cookie 立即过期。
        # 模块不 delete_cookie、不 import SESSION_COOKIE（否则既知道 Cookie
        # 名字、又得自己处理 __Host- 前缀，两套策略迟早漂移）。
        logger.info("Logout: user_id=%s ip=%s", request.user["id"], request.client_ip)
        logout_user(request, store=database)
        return redirect("/")

    return router


def safe_next(raw) -> str:
    """把回跳地址规范化成**站内路径**；不合法一律返回空串。

    实现收敛到 Core 的 `core.http.safe_next_path`（全项目唯一一处），
    **不要**在模块里再写一份：多份实现必然严格程度不一（漏掉反斜杠或 TAB），
    而 `/\t/evil.com` 经浏览器解析（URL 标准会先剥离 TAB）就是 `//evil.com`
    —— 登录后跨站跳转。
    """
    if not isinstance(raw, str) or not raw:
        return ""
    value = safe_next_path(raw, default="")
    # Core 用 "/" 表示"回退到首页"；本函数的契约是用空串表示"没有合法目标"
    return "" if (value == "/" and raw.strip() != "/") else value


def _lock_message(lang, reason, retry_after: int):
    """限流提示：基础文案 + 由渐进 backoff 算出的等待时间。

    等待时间必须来自真实判定（`database.reserve_login_attempt` 的返回值）：把"24 小时"
    之类的数字硬编码进文案，改配置之后提示就在撒谎。
    """
    keys = {"global": "auth_err_global", "email": "auth_err_email_lock",
            "ip": "auth_err_ip_lock"}
    base = _message(lang, keys.get(reason, "auth_err_global"))
    minutes = max(1, (int(retry_after) + 59) // 60)
    return f"{base} {t(lang, 'auth_err_retry_after', minutes=minutes)}"


def _try_login(request, email, password, lang):
    """返回 `(message | None, retry_after)`；`message is None` 表示登录成功。

    流程（顺序本身就是安全语义）：

        1. 一次**短写事务**：三闸门判定 + 占位记账（并发不可能超发）
        2. 释放 SQLite 写锁，再做昂贵的 scrypt 校验（不占锁）
        3. 成功 -> 一次短写事务收尾（清失败流水 + 审计 + 可选 rehash）

    **闸门只约束"错误的凭据"，绝不阻断正确的凭据。** 闸门命中时也照常校验密码：

    - 密码正确 -> 放行，并由 `complete_login_success()` 清空该邮箱的失败流水，等于当场
      自解封；
    - 密码错误 -> 才按闸门返回冷却提示。

    这条规则防的是"自锁死闭环"：若闸门命中就立刻返回，而失败流水唯一的清空入口又是
    "登录成功"，那么几个请求就能把任意已知邮箱锁到最长 24 小时 —— 受害者拿着正确密码
    也进不来，站内还没有解锁入口。

    闸门期的失败**不补记账**：补记账唯一的好处是"计数在冷却期内继续增长"，而冷却到期后
    那次 `allowed=True` 的尝试本来就会正常记账、冷却照旧逐次加深；代价却是每个公开可达
    的失败请求都要再取一次全局写锁（此刻已经付过一次 scrypt）。

    代价：闸门期内的错误尝试仍要付一次 scrypt。这本来就是爆破必须支付的单价，换来的是
    "合法用户永远不会被锁在门外"。
    """
    ip = request.client_ip or "unknown"
    limits = login_limits()
    if len(email) > EMAIL_MAX or not email or len(password) > PASSWORD_MAX:
        return _message(lang, "auth_err_creds"), 0

    # 1) 判定 + 占位：拆成"读计数—校验—记账"三步时，并发请求会同时读到低计数
    #    而全部放行（中间还夹着 scrypt），因此判定与记账必须在同一个事务里。
    allowed, reason, retry_after = reserve_login_attempt(
        email, ip,
        max_email_failures=limits["max_email_failures"],
        email_window_seconds=limits["email_window_seconds"],
        max_ip_failures=limits["max_ip_failures"],
        ip_window_seconds=limits["ip_window_seconds"],
        max_global_failures=limits["max_global_failures"],
        global_window_seconds=limits["global_window_seconds"],
    )

    # 2) 写锁已释放，才做密码校验（scrypt 约 240ms，不能让写锁陪着等）
    user = get_user_by_email(email)
    ok, rehashed_hash = verify_credentials(user, password)
    if ok:
        # 3) 收尾：清该邮箱失败流水 + 记成功 +（必要时）落新哈希，同一事务
        complete_login_success(email, ip, user_id=user["id"],
                               new_password_hash=rehashed_hash)
        login_user(request, user["id"], store=database)   # 轮换会话 + 写 Cookie（store 注入）
        if not allowed:
            logger.warning("Login succeeded despite the %s gate (valid credentials); "
                           "failure history cleared: user_id=%s ip=%s",
                           reason, user["id"], ip)
        else:
            logger.info("Login succeeded: user_id=%s ip=%s", user["id"], ip)
        return None, 0

    # 账号不存在时也做一次等量哈希校验，弱化计时侧信道（Core 之外只此一处）
    if user is None:
        dummy_verify(password)

    if not allowed:
        # 闸门命中：只返回冷却提示，**不补记账**（见 docstring 的说明）。
        # 三个闸门都记 WARNING：它们是"有人在爆破"的唯一信号来源
        logger.warning("Login blocked (%s failure limit): ip=%s retry_after=%ss",
                       reason, ip, retry_after)
        return _lock_message(lang, reason, retry_after), retry_after

    # 失败无需再记账：第 1 步的占位就是这次失败
    logger.info("Login failed: ip=%s", ip)
    return _message(lang, "auth_err_creds"), 0


def _try_register(request, lang):
    """返回 None 表示注册成功；否则返回错误文案。"""
    if not registration_enabled():
        return _message(lang, "auth_err_register_disabled")

    nickname = request.form.get("nickname", "").strip()
    email = normalize_email(request.form.get("email", ""))
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not nickname or not email or not password:
        return _message(lang, "auth_err_required")
    if len(nickname) > NICKNAME_MAX:
        return _message(lang, "auth_err_register_failed")
    if len(email) > EMAIL_MAX or "@" not in email:
        return _message(lang, "auth_err_register_failed")
    if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
        return _message(lang, "auth_err_password_range")
    if password != confirm:
        return _message(lang, "auth_err_password_mismatch")
    if is_weak_password(password, email=email, nickname=nickname):
        return _message(lang, "auth_err_password_weak")

    limits = register_limits()
    ip = request.client_ip or "unknown"
    # 只在只读连接上挡住"已经明显超限"的请求：注册是公开路径，不能让被限流的
    # 洪水白白占着全局写锁；判定权威仍是下面的原子事务（并发下不会超发）。
    if register_attempt_exceeded(ip, max_per_ip=limits["max_per_ip"],
                                 window_seconds=limits["window_seconds"]):
        logger.warning("Registration rate limit hit (pre-check): ip=%s", request.client_ip)
        return _message(lang, "auth_err_register_rate")
    if not try_register_attempt(ip,
                                max_per_ip=limits["max_per_ip"],
                                window_seconds=limits["window_seconds"]):
        logger.warning("Registration rate limit hit: ip=%s", request.client_ip)
        return _message(lang, "auth_err_register_rate")

    if get_user_by_email(email):
        logger.info("Registration rejected (email already registered): ip=%s",
                    request.client_ip)
        return _message(lang, "auth_err_register_failed")
    try:
        new_user_id = create_user(nickname, email, hash_password(password))
    except IntegrityError:
        # 并发注册撞上同一邮箱：UNIQUE 约束给出最终判定，返回友好提示而不是 500
        logger.info("Registration rejected (email already registered): ip=%s",
                    request.client_ip)
        return _message(lang, "auth_err_register_failed")
    logger.info("Registration succeeded: user_id=%s ip=%s",
                new_user_id, request.client_ip)
    return None
