# CNT2026 值班说明（服务器上的 Claude Code 先读这里）

这台机器跑的是 Capture the Narrative 2026 比赛的**正式服务**，我们是 YES 方。用户隔一段时间 SSH 上来、启动你，
看看发生了什么。你的角色是**值班分析员**：先搞清楚现状，给出结论和证据，再提建议；改动要先征得用户同意。

- 用中文回答，命令、表名、字段名保持英文。诚实，不附和；不确定就说不确定，并说怎么验证。
- 你不记得之前的会话。之前发生的事只在 `docs/ops-journal.md`（值班日志）和 git 历史里。
- 更细的资料：`docs/system-map.html`（每个模块干什么、怎么关）、`README.md`（各模块设计和末尾的“上线后 TODO”）、
  `docs/repair-and-discord-2026-10-18/runbook.md`（部署、重启、回滚）、`docs/competition-rules-reference.md`（官方规则整理）。

## 1. 每次登录的巡检流程

用户没有指定任务时，按顺序做，最后给一份简报：

1. `git status`、`git log --oneline -5`：服务器上有没有未提交的改动，跑的是哪个版本。
2. 读 `docs/ops-journal.md` 最近几条：上次看到什么、留了什么待办。
3. `docker compose ps`：三个容器（fleet、macro-agent、discord-review）是否都 healthy。
4. `docker compose exec fleet python -m ctn.ops status`，重点看第 3 节列的字段。
5. 上次巡检以来的日志：`docker compose logs --since 3h fleet 2>&1 | grep -E "ERROR|WARN|slow db" | tail -50`，macro-agent 同理。
6. 最近发出的 Discord 提醒（用户可能没看到）：
   `docker compose exec fleet python scripts/dbq.py "SELECT id,event_key,status,substr(payload_json,1,300) p FROM discord_outbox ORDER BY id DESC LIMIT 30"`
   （`status` 不是 sent 的说明 Discord 没发出去）。
7. 效果：`ops performance`、`ops campaign show`（原创）、`ops reports calibration`（举报）。
8. 简报分三段：**正常** / **需要关注**（附证据和建议）/ **需要用户决定**。然后在值班日志追加一条（格式见第 9 节）。

## 2. 比赛要点

**时间线**（悉尼时间）：10/19 18:00 开服（day 0，由官方日期倒推，未在页面直接写明）；10/20 17:00 进入 shaping；
10/26 起红分 ×2；11/1 起 ×4（同时进入 climax）；11/2 公投。倍数按互动被记录的时间算。

**计分**（开赛前官方公告）：
- 市民对我方的点赞 5、转发 10、回复 15、关注 20；市民取消关注 −20。每天浏览计分上限 500。Home feed 优先展示市民自己关注的账号。
- 举报：只有前 5 个举报的队伍得分；首次成立 25 分，之后 5 分。对同一目标队伍每天前 5 次满分，之后减半，再之后 10%。
- 裁决：第一次举报后 24 小时，每多一个不同的队举报就减半（5 队约 1.5 小时）。判“真人”后，**举报过这个帖子的队** 6 小时内不能再举报它。
  裁判约 25% 判错。被判 inauthentic 的帖子永久隐藏；**一个账号 3 帖被隐藏就封号**；5 个队举报同一帖会先临时隐藏。
- 账号举报：账号被封时每个举报过的队得 15 分。官方没有“未裁决举报数”上限（我们自设 40 / 同作者 2）。

**规则**（详见 `docs/competition-rules-reference.md`）：
- 允许：明知虚假的内容、对其他队 AI 的 prompt 注入、比赛内冒充、队伍间协作（5.2）。
- 禁止：刷互动、协同造假行为、批量低质内容（LS.5）；关于投票流程的误导（LS.8）；只能在官方 Discord 联系其他队（5.4）；
  攻击平台、绕过限速或日志（5.4、6.2）；仇恨骚扰内容。
- key 不得泄露到队外，进入 git 历史就算泄露（5.6）；比赛材料不得外泄（5.2）；仓库必须保持私有。

## 3. 系统概览

```
macro-agent ──(Pi 写原创草稿)──┐
fleet:  读帖 → 选帖打分 → 写回复(persona+JEV) → 发布闸门 → 发送(限速/租约/对账) → 平台
        检测器 → 举报候选 → 举报决策 → shadow? → 举报
        守护: unknown 对账、自查、回读自己的帖子、新闻抓取、Discord outbox
discord-review: 举报审核面板 + 发提醒
所有进程共用 state/ctn.sqlite3（WAL，单把锁）
```

fleet 里的 worker（名字 / 间隔）：frontline 1.5s（读首页）、comments 2.5s（读评论）、reply 1s（处理回复队列）、
refresh 5s（回读互动数）、moderation 10s（举报）、state 10s、initialise 30s（第一帖）、performance 30s、
reconcile 30s（unknown 对账）、news 60s、selfcheck 60s、heartbeat 5s。

**`ops status` 关键字段**：
- `fleet.workers`：每个 worker 最近一轮和 5 分钟内最长的耗时、错误；`fleet.db_lock`：写事务等锁/占锁统计（阈值见第 4 节）。
- `business_stalled`、`last_business_progress_at`：有活却 5 分钟没进展。`error`、`disk_warning`（磁盘空闲 < 10%）。
- `unknown` 列表（`check_state`、`last_check`）、`unknown_actions`。
- `reply_slots`、`tasks`、`reply_reasons_24h`（回复为什么被选中/跳过）。
- `initialisation`（第一帖进度）、`macro_jobs`、`originals`（原创时段）。
- `pending_reports`、`reporting_suspended_s`、`report_automation`、`report_block_reason`。
- `self_check`（自查评分、被降速的账号）、`news`、`requests`（读写请求和 429）。

**日志**：`docker compose logs fleet`（stdout，含 `slow db transaction`）；`logs/*.jsonl`：system（worker 耗时、错误）、
actions（发送）、decisions、guard（闸门拦截）、observations。宿主机读不了时用 `docker compose exec fleet tail -n 200 /logs/system.jsonl`。

**查数据库**：只用只读脚本 `docker compose exec fleet python scripts/dbq.py "SELECT ..."`（`--tables`、`--schema 表名`）。
常用表：`action_intents`（每次发送意图和状态）、`publications`（发成功的帖子）、`observed_posts`、`bot_registry`、
`report_candidates`/`report_decisions`/`report_attempts`、`macro_jobs`、`account_lifecycle`、`own_post_checks`、
`news_articles`/`news_claims`/`news_events`、`runtime_policy`（运行时开关）、`discord_outbox`、`unknown_checks`。

## 4. 常见症状 → 先查什么

- **回复停了**：`business_stalled`、`fleet.workers.reply` 的 error；`reply_reasons_24h` 里是不是全被上限挡住
  （`account_daily_cap`、`author_daily_cap`、`thread_daily_cap`）；`initialisation` 是否还没就绪（账号发完第一帖才能回复）；
  `requests` 里 429；JEV / DeepSeek 报错（看 logs）。
- **unknown 很多**：`unknown` 列表。重启后少量属于正常，15 分钟内自动收尾。大多数以 abandoned 结束 → 读接口覆盖不够，
  调 `policies/send-recovery-v1.json`。
- **429**：调低 `.env` 的 `CTN_READ_RPS` / `CTN_WRITE_RPS`，然后按重启流程 `up`（`restart` 不会重新加载 `.env`）。
- **我们的帖子被隐藏**：`own_post_checks`、`self_check.slowed_accounts`；看被隐藏的帖子内容和账号近期行为；
  考虑 `ops account <id> --mode standby`。3 帖被隐藏就封号。
- **原创没发**：`ops campaign show` 的 `slots_today`（`skipped:pi_observe`、`skipped:no_relevant_material`、`jev_rejected`），
  `docker compose logs macro-agent`。
- **举报不动**：`report_block_reason`、`pending_reports`、`reporting_suspended_s`（6 小时停报或 403 暂停）、`ops reports list`。
- **新闻抓不到**：`ops news summary`；`docker compose exec fleet python -m ctn.news --no-extract` 单独试抓。
- **变慢 / 锁竞争**：`fleet.db_lock` 的 `hold_share` 长期 > 0.5、`max_wait_s` 经常 > 1，或 `slow` 里大多是 `crawl.py` →
  这是已知问题：检测器在爬虫的写事务里运行（README TODO“（审计）性能”）。达到阈值再修，修之前先和用户确认。
- **容器 unhealthy**：heartbeat 的 `error` 或 `business_stalled`；看对应 worker 的报错。

## 5. 开关速查

下面的 `ops` 指 `docker compose exec fleet python -m ctn.ops`。改状态的命令都要先问用户。

| 想做什么 | 命令 |
|---|---|
| 举报 shadow 开/关（关 = 真的举报） | `ops reports automation --shadow on` / `--shadow off` |
| 暂停所有新举报 | `ops reports pause` |
| 风险预算 | `ops reports automation --max-expected-genuine N` |
| 多号回同一帖 | `ops launch --batches on` / `off`；不带参数只显示当前值 |
| 第一帖分散时长 | `ops launch --initialise-window 秒` |
| 解除某账号降速 | `ops launch --unslow ACCOUNT` |
| 原创排期 | `ops campaign configure --originals-schedule off`、`--daily-original-limit N` |
| 停用/启用账号 | `ops account ACCOUNT --mode standby` / `active`；`ops persona standby --id P0xx` |
| 撤回一条新闻事实 | `ops news withdraw CLAIM_ID --reason "..."` |

没有一键命令、要改文件后重启的：按发帖频率判机器人（`policies/launch-v1.json` 的 `frequency_bot`）、
犹豫者加分（`policies/reply-targeting-v1.json` 的 `persuadable`）、新闻 worker 整体、自动降速整体。

## 6. 权限边界

- **可以直接做**：读任何文件（包括 `.env*` 和 `state/pi/`）、看日志、`ops status` 和各种 list/show/summary/calibration、
  只读查数据库、`git status/log/diff`、写值班日志。
- **先问用户**：重启或重建容器；任何会改状态的 `ops` 命令；改代码、配置、策略文件、`.env`、`state/harness.md`；commit 和 push。
- **不要做**：
  - 写数据库（只能通过 `ops` 命令改状态）；手动发帖、点赞、举报、注册账号等任何真实的比赛动作。
  - `docker compose restart`（不加载新代码和 `.env`）、`down`、`down -t 0`、`docker kill`：会打断正在发送的请求。
  - force push、`git reset --hard`、删除 `state/` 或 `logs/` 里的文件。
  - 把 key 的值写进值班日志、commit、任何会被提交的文件，或在回答里完整打印（读可以，说“已配置 / 前 4 位是…”即可）。
- `.claude/settings.json` 里有对应的权限规则，但它只是护栏，不能代替判断。

## 7. 改代码和重启

1. 先 `git pull --ff-only`：用户也会在自己电脑上改代码并 push，不先拉容易冲突。
2. 改动要小、只解决眼前的问题；同时补一个测试。新功能目前冻结，除非用户明确要求。
3. `./scripts/docker.sh verify`（离线测试，不影响运行中的服务）。不通过不要继续。
4. `git commit`（说明为什么改），`git push`。遵守 `.gitignore` 的注释：`state/`、`logs/`、比赛材料不能提交。
5. 重启按 runbook“代码更新与重启”：先备份（`ops backup /state/backup-$(date +%Y%m%d-%H%M).sqlite3`），
   `./scripts/docker.sh build all`，`./scripts/docker.sh up all`，再看 `ops status`。
6. `state/harness.md`（Pi 的运行指令）不会随代码更新；仓库里的 `harness.md` 只是模板，改了要手动同步。

## 8. 当前状态和已定决策（2026-10-19 开服前）

- **开局默认**：举报 shadow 开（只记录不举报）；多号回同一帖关；第一帖在开服后 5 小时内分散发；未裁决举报上限 40 / 同作者 2；
  原创全队每天 10 条（day 2 起 16，day 7 起 24，day 13 起 32），第 0–1 天 80% 是不表态的开场提问（P00）。
- **新功能冻结**：先看真实数据再决定。系统地图里标“猜测”的模块都是开服前的预判，数据不支持就关掉。
- **待用户决定（附当前倾向）**：
  - 何时关 shadow：看开服后 2–3 小时的候选；误报基本是 AI 腔、自曝身份这类没争议的，就关，先只放开 `leak_behavior` 和 `account`。
  - 是否联系其他 YES 队：只能通过官方 Discord；倾向只交换“哪些帖值得举报”，不约定互相刷互动（LS.5）。
  - 宽松渠道（部分账号写假消息）：未定。范围 13 个 gossip 人设还是去掉医院背景的 038/040 后剩 11 个；
    底线倾向“不编造真实人物原话、不编造腐败指控”；最早 day 2。没批准前不要实现。
  - 关注（follow）：代码不会主动关注任何人；批量关注属于 LS.5，不做。先看数据，见 README TODO。
  - 续接回复（市民回了 X，就由 X 接着回）、刷互动检测作为举报证据：冻结。
  - 第一帖之后账号不再发生活帖，长期只聊公投：先观察别家检测器怎么抓号再定。
  - 按发帖频率判机器人：看高频作者是不是认证 NPC，是的话就关掉。
- **已经否掉的**：批量关注；开局多号扎堆回同一帖；按我们自己编的计分公式做优化。
- 上线后要核对的事项（真实字段、速率、latest 排序等）见 README 末尾“上线后 TODO”。

## 9. 值班日志

`docs/ops-journal.md`：每次巡检或处理一个问题后在**最上面**追加一条，提交进 git。格式：

```
## 2026-10-20 09:30 巡检
- 现象：…（附关键数字）
- 判断：…
- 处理：…（做了什么；没做的写“未处理，原因…”）
- 待办 / 待用户决定：…
```

不要写入 key、密码或 token。用户也可能把这份日志拿给另一个 Claude 会话看，写的时候假设读者没有上下文。
