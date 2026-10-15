# 运行与运维

## 目录与权限

代码、模板、配置**只读**；只有数据/日志/备份可写。这样即使将来出现代码执行，
攻击者也改不了代码、写不进后门：

```
/srv/elenvind              代码 / 配置 / 模板 / .venv     root:elenvind 只读（750）
/srv/elenvind/data         sqlite.db 及 -wal/-shm         elenvind 可写（750）
/srv/elenvind/logs         app.log                        elenvind 可写（750）
/srv/elenvind/backups      --backup 的默认输出             elenvind 可写（750）
```

一次性准备：

```bash
sudo useradd --system --home /srv/elenvind --shell /usr/sbin/nologin elenvind
sudo chown -R root:elenvind /srv/elenvind
sudo chmod 750 /srv/elenvind && sudo chmod 640 /srv/elenvind/config.toml
sudo install -d -o elenvind -g elenvind -m 750 /srv/elenvind/{data,logs,backups}

# 单元文件随包分发，`--with-unit` 会自动装好它（推荐）
sudo /srv/elenvind/.venv/bin/elenvind proxy configure --domain <域名> --apply --with-unit
# 或者手工复制：包内路径是 <venv>/lib/python3.*/site-packages/elenvind/resources/elenvind.service
sudo cp "$(dirname "$(readlink -f /srv/elenvind/.venv/bin/elenvind)")/../elenvind/resources/elenvind.service" /etc/systemd/system/
sudo cp manual/logrotate.conf /etc/logrotate.d/elenvind
sudo systemctl daemon-reload && sudo systemctl enable --now elenvind
systemd-analyze verify /etc/systemd/system/elenvind.service    # 期望无输出
```

单元文件里**不能有行尾注释**：systemd 只把行首 `#` 当注释，行尾 `#` 会把后面的
文字并进参数值，于是那条指令被静默忽略。`tests/test_operations.py` 有守卫。

## 升级

1. **先备份**：`.venv/bin/elenvind backup`
2. 覆盖代码（不要覆盖 `config.toml`、`data/`、`logs/`、`backups/`）；
3. `sudo systemctl restart elenvind`；
4. 看日志确认 `Startup complete`，并冒烟 `GET /`、`GET /robots.txt`。

数据库结构迁移在**启动时**自动执行，整个过程在 `flock` + `BEGIN IMMEDIATE`
里完成（`user_version` 与数据搬迁同一事务），因此：

- 多个 worker 同时启动不会重复迁移；
- 中途失败整体回滚，不会留下"迁移了一半"的库。

### 破坏性迁移会自动留快照

某些迁移会**改写内容且不可恢复**（例如 v6 把"软删除但正文仍在"的评论
就地涂黑）。执行这类迁移前，本服务会先写一份数据库快照：

```
data/sqlite.db.pre-v6-20260101-040000.db      # 权限 0600，与库同目录
```

**快照写不出来（磁盘满 / 权限不对）就中止迁移并拒绝启动** —— 绝不"先毁数据再说"。
确认新版本运行正常后可以自行删除这些快照；要回滚就把它放回 `data/sqlite.db`。

### 回滚（把代码换回旧版本）

如果你把服务**降级**到更旧的版本，而数据库结构已经是新版本，服务会**拒绝启动**
并明确说明（`数据库 schema 版本…高于本代码支持的版本`）。这是刻意的：旧代码
不认识新结构，继续跑只会以每请求报错的形式失败，甚至把数据写坏。

正确的回滚姿势：

1. 停服；
2. 用上面那份 `pre-vN` 快照（或升级前的备份）替换 `data/sqlite.db`；
3. 换回旧代码并启动。

迁移历史留下的 `*_legacy` 表会被识别为"上次迁移未完成"，同样**拒绝启动**并提示
你先检查备份表再手工清理（绝不静默继续）。

## 备份与恢复

```bash
.venv/bin/elenvind backup                       # → backups/elenvind-<时间戳>.db
.venv/bin/elenvind backup --to /srv/backups/x.db
```

- 走 SQLite 的在线备份 API（WAL 模式下 `cp sqlite.db` 会丢掉还在 `-wal` 里的事务）；
- 目标文件**从创建那一刻起**就是 `0600`（库里含密码哈希与会话令牌）；
- 落盘后做 `PRAGMA quick_check`；失败会删掉半成品并返回非零退出码；
- **拒绝覆盖已存在的文件** —— 所以失败/被杀留下的半成品会挡住下一次备份，
  删掉或改名即可。

放进 cron：

```cron
0 4 * * * cd /srv/elenvind && .venv/bin/elenvind backup >> logs/backup.log 2>&1
```

恢复（可重复演练）：

1. 停服：`sudo systemctl stop elenvind`
2. 把当前库**及它的 `-wal` / `-shm`** 一起挪走（它们是旧库的状态，
   留在原地会被当成新库的一部分）：
   `mv data/sqlite.db* /tmp/broken/`
3. 把备份放到 `data/sqlite.db`，并让三个文件的权限**一致**：
   `chmod 640 data/sqlite.db`（`-wal`/`-shm` 由应用重建）
4. 启动：`sudo systemctl start elenvind`，确认 `GET /` 200、评论数符合预期。

> `attempt to write a readonly database` = `sqlite.db`/`-wal`/`-shm` 权限不一致
> 或被挂载成只读。表现为"写请求 500、读请求正常"；权限恢复一致后应用自愈。

## 日志

- 应用自己逐请求记一行（不含查询串、请求体、Cookie 与凭据）；
- 默认 `[logging].rotate = false`：只写当前文件，由 `logrotate` 轮转。
  多 worker 下应用内轮转会让多个进程互相截断，因此**不推荐**打开；
- 应用使用 `WatchedFileHandler`：文件被轮转（改名）后会自动重新打开，无需信号；
- 建议同时保留反代的访问日志，用于排查连接层问题。

## 限流与账号运维

- 登录闸门**只约束错误凭据**：密码正确永远能登录，且成功登录会当场清空该邮箱的
  失败流水；被闸门拦下的尝试**不写库**。
- 想立刻清空某个邮箱的失败流水：

  ```bash
  sqlite3 data/sqlite.db "DELETE FROM login_attempts WHERE email='x@y';"
  ```

- 评论"已达上限"：该文章未涂黑的评论达到 `max_comments_per_article`。管理员涂黑
  旧评论即可释放名额（涂黑是把正文在库里永久替换成黑块，不可恢复）。
- 限流流水表（`login_attempts` / `register_attempts` / `comment_rate`）在启动时清理，
  运行期每小时机会式清理一次；清理**失败会下一个周期重试**（不会被静默跳过）。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| 登录提示"稍后再试" | 触发登录闸门。**用正确密码仍可登录**；错误密码需等 `Retry-After` 秒。要立刻清空流水见上一节 |
| 表单提交返回 400 | CSRF 令牌与 Cookie 不匹配（页面开太久 / 清过 Cookie）。刷新页面重试即可，页面会给出提示 |
| 返回 413 / 411 / 415 | 请求体超 `max_body_size` / 表单方法没带 `Content-Length` / Content-Type 不在白名单 |
| 写请求 500，日志出现 `attempt to write a readonly database` | `data/` 权限不一致或挂载只读。读请求在此期间仍正常，权限修好后自愈 |
| 页面没有样式 | `[static].css` 未配置，或配置的地址不可达，或该来源没加进 `[security.csp] style-src`。本服务**不再内置样式表**，外观由你自己托管 |
| 图片/图标 404 | 配置里写的是站内相对路径（如 `/imgs/logo.png`），但本服务不发出静态文件。把它换成你自托管后的完整 URL |
| 浏览器里能登录、下一秒又变未登录 | `cookie_prefix = true` 但应用不知道自己在 HTTPS 上。把 `site_url` 写成 `https://…`，或在 `trusted_proxies` 里列出反代（配置阶段就会拒绝这种组合，启动日志也会点名） |
| 启动报"数据库 schema 版本高于本代码支持的版本" | 你把服务降级了。按上一节回滚流程用快照/备份恢复 |
| 启动报"检测到上次迁移未完成" | 库里有 `*_legacy` 备份表。先备份再按提示处理，绝不静默继续 |
| 启动报"v4 邮箱小写化会撞上 UNIQUE 约束" | 库里存在忽略大小写后重复的邮箱（早先版本按大小写区分账号）。按提示合并/改名后重启；迁移中止时不改动任何数据 |
| 启动报"模板编译失败" | 模板语法错误。**启动期就会失败**，而不是等第一个页面 500 |
| 浏览器报"连不上"且日志里没有请求 | 反代没转发、或应用没起来（看 `journalctl -u elenvind`）。应用只监听回环，直接访问公网 IP 打不到它 |
| 站点在 HTTPS 下登录后立刻变未登录 | `site_url` 与实际协议不一致（应是 `https://…`），或反代没转发 `X-Forwarded-Proto` 而 `site_url` 又声明了 http |
| 反代返回 413 但应用日志里没有这条请求 | 代理的请求体上限小于应用的 `max_body_size`（nginx 默认只有 `1m`），请求被代理抢答了 |
| 启动报"破坏性迁移前的自动快照失败" | 备份目录不可写或磁盘满。修好后重启即可 |
| 文章/页面改了不生效 | 内容缓存最多 2 秒节流；也可重启或等待自动重扫（日志会记 `Article directory changed`） |
| 日志里出现 `Write lock wait N ms` | 有写事务等待了较久。偶发正常；持续出现说明写路径被热点占用或外部进程（如 `sqlite3` shell）长期持有写锁 |
| 非回环地址监听告警 | 你 bind 到了 `0.0.0.0` 且信任转发头：端口可被直连，绕过 TLS/HSTS/边缘限速。改回 `127.0.0.1` 并让反代回源，或给端口加防火墙 |

## 建议监控的最小集

- 启动失败（进程反复重启）——迁移拒绝、模板错误、配置非法都会在这里先出现；
- HTTP 5xx 比例；
- 日志关键字：`Write lock wait`、`Destructive migration ahead`、`Login blocked`、
  `Migration failed`、`Opportunistic prune … failed`；
- 磁盘剩余空间（备份与日志是同机增长的唯一来源）。
