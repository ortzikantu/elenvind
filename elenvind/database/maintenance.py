"""限流流水表的**有界增长**：启动清理 + 低频机会式清理。

三个限流表（`login_attempts` / `register_attempts` / `comment_rate`）都是
"每次尝试写一行"，只在进程启动时清理会让长跑进程无界增长：

    一张每次登录失败写一行的表，在长期运行的站点上最终会有几百万行，
    而窗口查询虽然走索引，表文件与其 WAL 仍会持续膨胀。

但也不能把清理放进每次写入的事务里：`DELETE ... WHERE attempted_at < ?`
是 O(表大小) 的操作，放在写锁内会明显拉长锁持有时间（高并发互相排队），
而逐表各写各的清理策略迟早互相矛盾。

这里给出统一策略：**时间片**清理。只有当距上次清理超过 `interval_seconds`
时才真的执行 DELETE；调用点在写事务**之外**，因此不占写锁。

调用位置是硬约束：`prune()` 自己就是一个写事务（走 `write_tx()`，取同一把
flock），所以**绝不能**在另一个 `write_tx()` 块内调用它 —— 那会触发重入守卫
（否则就是自死锁）。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: 默认清理间隔：每小时一次。窗口最长不过几天，保留期以天计，
#: 因此这个频率足以把表压在几十万行以内（实际远小于此）。
DEFAULT_PRUNE_INTERVAL = 3600

_lock = threading.Lock()
#: {表名: 上次**尝试**清理的时间戳} —— 记的是"尝试"而不是"成功"，
#: 因此失败后下一个间隔会重试，而不是整整一个周期不再清理（见 prune()）。
_last_attempt: dict[str, float] = {}


#: **允许清理的 (表名, 时间列)** 白名单。
#: 动态标识符（表名/列名不能走参数绑定）只允许来自这里：
#: 调用方传入其它组合会直接 `ValueError`，而不是把外部字符串拼进 SQL。
PRUNABLE = frozenset({
    ("login_attempts", "attempted_at"),
    ("register_attempts", "attempted_at"),
    ("comment_rate", "attempted_at"),
})


def _validate_target(table: str, column: str) -> None:
    """只接受白名单内的表/列组合（防 SQL 注入的"标识符"面）。"""
    if (table, column) not in PRUNABLE:
        raise ValueError(
            f"prune() 不接受 {table!r}.{column!r}：只允许 "
            f"{sorted({t for t, _ in PRUNABLE})} 的 "
            f"{sorted({c for _, c in PRUNABLE})} 列（见 PRUNABLE 白名单）")


def reset_state() -> None:
    """清空"上次尝试清理"记录（测试用，避免用例之间互相影响）。"""
    with _lock:
        _last_attempt.clear()


def prune(table: str, column: str, days: int,
          interval_seconds: int = DEFAULT_PRUNE_INTERVAL) -> int:
    """机会式清理：到期才删除 `column` 早于 `days` 天的行，返回删除行数。

    调用方必须在**写事务之外**调用它（例如写入提交之后），
    这样 DELETE 不会延长原事务的写锁持有时间。

    它自己是一个写事务（`write_tx()` → flock + BEGIN IMMEDIATE），因此
    **在另一个 write_tx() 块内调用会立刻触发重入守卫**。清理失败只记日志、
    不向上抛：这是"顺手的维护工作"，不该让业务写入失败（既有的容错语义）。

    计时语义（刻意）：`_last_attempt` 在**尝试删除之前**就更新，所以一次失败
    （磁盘满、外部写者占着 SQLite 写锁）之后要等下一个间隔才会重试。宁可晚一个
    周期再清，也不要让"清理失败"退化成每个请求都重试一次的高频写。
    """
    _validate_target(table, column)
    now = time.time()
    with _lock:
        attempted = _last_attempt.get(table)
        if attempted is not None and now - attempted < interval_seconds:
            return 0
        _last_attempt[table] = now
    from .transaction import write_tx

    try:
        with write_tx() as conn:
            cursor = conn.execute(
                # 表名/列名只可能来自 PRUNABLE 白名单（见 _validate_target），
                # 不是外部字符串拼接；参数走占位符。
                f"DELETE FROM {table} WHERE {column} < ?",
                (now - days * 86400,))
            removed = cursor.rowcount or 0
    except Exception:                                             # 清理失败只记日志
        logger.exception("Opportunistic prune of %s failed (will retry next "
                         "interval)", table)
        return 0
    if removed:
        logger.info("Pruned %s row(s) from %s", removed, table)
    return removed
