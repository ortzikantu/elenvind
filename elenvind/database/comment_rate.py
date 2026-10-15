"""评论发布限流（comment_rate 表）：短窗口计数，防刷屏。

策略与 db_login 同款（逐条流水 + 窗口统计），维度为：
- 单用户窗口内评论数（正常读者连发很少超过阈值）；
- 单 IP 窗口内评论数（防同一出口批量刷屏）。
窗口都很短（默认 60 秒），误伤概率低。

流水清理分两层（见 `database.maintenance`）：
- **启动时**全量清一次（`cleanup_old_comment_attempts`）；
- **运行期**每次发布提交之后，机会式清理（每小时最多一次），
  这样长跑进程不会无界增长，又不会把 O(表大小) 的 DELETE 放进写事务里。

并发说明：限流判定、父评论校验与评论写入必须在一个事务里完成才真正原子，
因此对外主入口是 try_post_comment() —— 由它统一取号、计数、校验、插入。
该事务由 Core 的唯一写入口 `write_tx()` 提供（跨进程 flock + BEGIN IMMEDIATE +
提交/回滚/关闭）。

与清理的关系（重要）：`prune()` 自己也要写数据库（取同一把 flock），
因此**必须在本函数返回之后、`write_tx()` 块之外**调用 —— 块内调用会触发
重入守卫（`write_tx()` 不可嵌套），否则就是自死锁。
"""
import time

from .comment import comment_depth
from .maintenance import prune
from .transaction import write_tx

RETENTION_DAYS = 7


def cleanup_old_comment_attempts(days: int = RETENTION_DAYS):
    """启动时删除指定天数之前的流水，控制表体积。"""
    with write_tx() as conn:
        conn.execute("DELETE FROM comment_rate WHERE attempted_at < ?",
                     (time.time() - days * 86400,))


def try_post_comment(article_slug: str, user_id: int, ip: str, content: str,
                     parent_id=None, *, max_per_user: int, max_per_ip: int,
                     window_seconds: int, max_per_article: int,
                     created_at: str, max_depth: int = 0) -> str:
    """原子地校验父评论与深度、判定限流并写入评论。

    返回 "ok" / "rate_user" / "rate_ip" / "too_many" / "bad_parent" / "too_deep"；
    被拒绝时不写流水、不写评论（避免把攻击流量放大成表增长）。

    父评论校验（`parent_id` 非空时）：父评论必须存在，且必须属于**同一篇文章**
    （防止在 A 文章下引用 B 文章的评论 id）。刻意**不校验** `is_deleted`：删除是
    "涂黑"而非物理删除，已删除评论仍可被回复，关系链保持完整。

    深度校验（`parent_id` 非空且 `max_depth > 0`）：父评论层级 + 1 不得超过
    `max_depth`。模板只在未到顶时渲染回复按钮，但 `reply_to` 是表单字段、可以被直接
    构造，因此这里是**唯一的权威判定点**。判定必须在事务内：路由层的预检在写事务之前，
    属于 TOCTOU —— 两个并发请求可以同时看到"还没到顶"，然后各写一层。

    所有校验都在 BEGIN IMMEDIATE 之后、INSERT 之前，因此限流计数、深度判定与写入同处
    一个事务：要么全部生效，要么全部回滚。被拒绝的分支显式 `rollback()`，保持"什么都
    没发生"。

    事务内**不做**流水清理：那是 O(表大小) 的 DELETE，放进写锁会明显拉长锁持有时间，
    而且它自己也是一个写事务（嵌套取锁）。清理只由启动时的
    `cleanup_old_comment_attempts()` 与提交后的机会式 `prune()` 负责 —— 限流窗口只有
    几十秒，保留期是 7 天，残留旧流水不影响判定的正确性。
    """
    now = time.time()
    cutoff = now - window_seconds
    with write_tx() as conn:
        user_count = conn.execute(
            "SELECT COUNT(*) FROM comment_rate WHERE user_id = ? AND attempted_at > ?",
            (user_id, cutoff)).fetchone()[0]
        if user_count >= max_per_user:
            conn.rollback()
            return "rate_user"
        ip_count = conn.execute(
            "SELECT COUNT(*) FROM comment_rate WHERE ip = ? AND attempted_at > ?",
            (ip, cutoff)).fetchone()[0]
        if ip_count >= max_per_ip:
            conn.rollback()
            return "rate_ip"
        # 配额只统计**仍然展示中**的评论（涂黑即释放名额）。
        # 涂黑之所以保留行，是为了不拆散评论树；但把已涂黑的行算进配额，
        # 就等于"上限 = 该文章永久停止接受评论"——管理员也救不回来
        # （全项目没有任何 DELETE 评论的路径）。所以这里必须排除 is_deleted=1。
        article_count = conn.execute(
            "SELECT COUNT(*) FROM comment WHERE article_slug = ? AND is_deleted = 0",
            (article_slug,)
        ).fetchone()[0]
        if article_count >= max_per_article:
            conn.rollback()
            return "too_many"
        if parent_id is not None:
            # 只取判定需要的列：parent_id 是外部输入，绝不能带入写入语句。
            parent = conn.execute(
                "SELECT id, article_slug, parent_id FROM comment WHERE id = ?",
                (parent_id,)).fetchone()
            if parent is None or parent["article_slug"] != article_slug:
                conn.rollback()
                return "bad_parent"
            if max_depth > 0:
                # 复用 database.comment.comment_depth（防环 + limit 兜底都在那里），
                # 传 conn 以确保回溯与 INSERT 处于同一事务。
                parent_depth = comment_depth(parent, max_depth=max_depth, conn=conn)
                if parent_depth + 1 > max_depth:
                    conn.rollback()
                    return "too_deep"
        conn.execute(
            "INSERT INTO comment_rate (user_id, ip, attempted_at) VALUES (?, ?, ?)",
            (user_id, ip, now))
        conn.execute(
            "INSERT INTO comment (article_slug, user_id, content, created_at, parent_id, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (article_slug, user_id, content, created_at, parent_id))
    # 写事务已结束（flock 已释放），才做机会式清理：
    # DELETE 不会占着刚释放的写锁，也不会嵌套取锁。
    prune("comment_rate", "attempted_at", RETENTION_DAYS)
    return "ok"
