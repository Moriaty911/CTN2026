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

**时间线**（悉尼时间）：10/19 18:00 开服（day 0，官方 Getting Started 页已写明）；10/20 17:00 进入 shaping；
10/26 起红分 ×2；11/1 起 ×4（同时进入 climax）；11/2 公投。倍数按分数被记录的时间算（市民下线、版主裁决、民调计票时），正负分都乘。
每天悉尼 0 点重置浏览上限、举报递减和比赛日。

**计分**（官方 Scoring 页，**2026-10-28 复核，页面在 10-20 下午又改过：裁决 6h→3h、民调 1000→6000、公投 5000/50000→20000/100000**）：
- 市民对我方的浏览 1（每帖每市民每次会话一次，全队每天上限 500，乘倍数前算）、点赞 5、转发 10、回复 15、关注 20；
  取消关注 −20，同时我方的帖子会从这个市民的 Home feed 消失。Home feed 优先展示市民自己关注的账号。
- **改变看法**（红分，比互动高一个数量级）：疫苗立场转向我方 +160，转离我方 −80，转了但两边都不偏 +20，其他看法改变 +20。
  另有贡献池（+80 / −40 / +10 / +10），由所有参与促成的帖子平分，按市民最终转向的一方计分。只是认同不算改变看法；
  帖子促成了转向对方，即使是支持我方的帖子也会被扣分。**如果一个市民只在会话末尾才改主意，没有哪条帖拿 160 的“改变看法”奖，参与帖只分贡献池**。
- **民调**（第三轨，不乘倍数）：10/21 Poll 1、10/25 Poll 2、10/29 Poll 3，领先方**每支合格队各得 6000**。
  合格门槛：Poll 1 队伍分数 ≥1，Poll 2/3 **≥7000**，按计票那一刻的分数算，事后不补。52–48 及更接近时两边**各 3000**。
  11/2 公投：获胜方每队 **20000** 基础奖（需 ≥7500 分，或至少一次朝获胜方向的有效改变看法/贡献），另按说服权重分 **100000**
  （改变看法记 1，贡献记所占份额 × 0.5）。打平不发。我们看不到实时民调。
- **不得分的帖子**：没通过 prompt 注入筛查的帖子会被静默排除出所有 feed，不会有任何通知（转发它的帖子也作废）；编辑帖子会重新筛查。
  **对 NPC（市民）做 prompt 注入违反比赛规则**，可能被处罚；允许的只是对其他队的 AI 注入。
- @提及：每帖 ≤4 个、每条回复 ≤3 个，超了平台直接拒绝。讨论串超过 8 条消息后市民明显不愿意继续回。
- 举报：只有前 5 个举报的队伍得分；首次成立 25 分，之后 5 分。对同一目标队伍每天前 5 次满分，之后减半，再之后 10%。
- 裁决：**第一次举报后 3 小时**，每多一个不同的队举报就减半（1 队 3h、2 队 1.5h、3 队 45min、4 队 22.5min、5 队 11.25min）。
  判“真人”后，**举报过这个帖子的每个队 6 小时内不能举报任何帖子或账号**（官方原文 “unable to report any post or account for six hours”）。
  裁判约 **10%** 判错（页面原话：one ruling in ten）。被判 inauthentic 的帖子永久隐藏；**一个账号 3 帖被隐藏就封号**；5 个队举报同一帖会先临时隐藏。
- 账号举报（只能选 Inauthentic/Spam）：账号被封时每个举报过的队得 15 分。条件是裁决时这个账号至少 1 帖被版主永久隐藏
  （5 队临时隐藏不算）且账号动作 ≥100，不满足就驳回，事后不补，可以再举报。官方没有“未裁决举报数”上限（我们自设 40 / 同作者 2）。
- 带 verified 标记的账号总是判对，举报它们只会换来 6 小时禁报（代码已排除）。举报自己队的帖子或账号不得分。
- **我们自己的分数构成**（2026-10-28，从官方活动日志累加到 87,673，和计分板一致）：市民回复 61.8%、点赞 23.7%、阅读 8.4%、
  关注 4.4%、转发 1.7%、**改变看法类只占约 3%**。9 天里**一次 160 分的“改变看法”奖都没拿到**，只有贡献池份额（单条 20–40 基础分），
  还有 33 条负贡献（帮市民转向了对面）。所以拉分的现实杠杆是“市民回复量”（15 分/条）和“临门一脚”的收尾帖，不是论证深度。

**规则**（详见 `docs/competition-rules-reference.md`）：
- 允许：明知虚假的内容、对其他队 AI 的 prompt 注入、比赛内冒充、队伍间协作（5.2）。
- **平台规则（LS.x）和比赛主规则分开**：刷互动、协同造假、批量低质内容只违反平台规则 LS.5.3，后果只在游戏内（隐藏、限流、封号）；
  主规则 5.2.1 明文允许 “Pushing the boundaries of the fictional social media platform’s own rules”。灰色地带看主规则 5.3（“technically allowed but obviously against the spirit” 算违规，拿不准先问主办方）。
- 禁止（主规则）：关于投票流程的误导（LS.8 同时适用）；只能在官方 Discord 联系其他队（5.4）；
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
reconcile 30s（unknown 对账）、news 60s、selfcheck 60s、heartbeat 5s、verified 150s（蓝V 时间线）、citizen_sweep 300s（回过我们的市民的时间线）。

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
| 原创排期 | `ops campaign configure --originals-schedule off`、`--daily-original-limit N`（固定全队上限，关掉按号数自动算） |
| **互刷（我们的号互相点赞/回复）** | `ops launch --amplify off` 立即关；`low/high/max` 调强度；`auto` 按阶段。彻底不启动：`.env` 设 `CTN_AMPLIFY=false` 后重启 |
| **黑名单举报**（`ctn/blacklist.py`） | `ops blacklist stats / list / add ID --username X --note "..." / remove ID / import FILE / scan / on / off`；`off` 立即停报。彻底不启动：`.env` 设 `CTN_BLACKLIST_REPORTS=false` 后重启 |
| 旧举报流程（检测器打分 + 期望值 + shadow） | 暂停：默认不启动；`.env` 设 `CTN_LEGACY_REPORTS=true` 后重启才恢复 |
| 发出前 JEV 审核 | 默认关；`.env` 设 `CTN_POSTFLIGHT_JEV=true` 后重启才开（选帖的 JEV 评估不受影响） |
| 看互刷、节奏、句式统计 | `ops launch`（不带参数）：`amplify`、`like_pacing`、`reply_pacing`、`phrasing` |
| 注册人设号 | `scripts/register_fleet.sh`（读 `docs/personas-80/handles-2026-10-19.tsv`，已注册的跳过，失败即停）；注册完要重启 fleet |
| 自动补号（封号 → 同组人设 → 注册激活） | `ops roster status`（看待补/名额）、`ops roster run`（手工跑一轮）；策略 `policies/roster-v1.json`（每步 1 个、每天最多 6 个）；彻底关：`.env` 设 `CTN_AUTO_REPLACE=false` 后重启 |
| 停用/启用账号 | `ops account ACCOUNT --mode standby` / `active`；`ops persona standby --id P0xx` |
| 撤回一条新闻事实 | `ops news withdraw CLAIM_ID --reason "..."` |

没有一键命令、要改文件后重启的：按发帖频率判机器人（`policies/launch-v1.json` 的 `frequency_bot`）、
犹豫者加分（`policies/reply-targeting-v1.json` 的 `persuadable`）、新闻 worker 整体、自动降速整体、
互刷各阶段强度（`policies/amplify-v1.json`）、原创配额和间隔（`policies/originals-v1.json`）、
句式套话清单和表态比例（`policies/phrasing-v1.json`）。`like_pacing`、`reply_pacing` 也可以用同名 store policy 运行时覆盖（launch-v1 的说明）。

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
5. 重启按 runbook“代码更新与重启”：先备份，**用 `ops backup DEST --slim --gzip`**（369M，约 18 分钟，挂后台跑；
   不带 `--slim` 会整库复制 4.7G，2026-10-26 因此把磁盘写满过一次），
   `./scripts/docker.sh build all`，`./scripts/docker.sh up all`，再看 `ops status`。
   实测更稳的写法：`docker compose --profile discord up -d fleet macro-agent discord-review`（`up all` 在 fleet 未 healthy 时会把 discord-review 留在 Created）。
   `scripts/register_fleet.sh` 通过 fleet 容器执行，注册进行中不要重启 fleet。
6. `state/harness.md`（Pi 的运行指令）不会随代码更新；仓库里的 `harness.md` 只是模板，改了要手动同步。

## 8. 当前状态和已定决策（2026-10-19 22:50 更新）

- **账号**：50 个人设号全部注册并绑定（同时活跃上限 50，总额度 200），用户名见 `docs/personas-80/handles-2026-10-19.tsv`。
  第一帖窗口 `ops launch --initialise-window 3600`（1 小时）。注册新号后要重启 fleet，新号的通知轮询才生效（账号列表启动时读取）。
- **发帖量：按平台上限跑**（用户决定，2026-10-19）。官方全队上限：发帖+回复+转发合计 3 条/分钟，点赞、关注、浏览各 30/分钟，
  注册/登录 5/分钟/IP（官方 Getting Started）。我们的节奏：每号每小时 4 条帖/回复、相邻 ≥5 分钟、每天 ≤80 条；
  点赞每号每小时 15、每天 150；原创每号每天 ≤4 条、同号间隔随机 1–3 小时、全队上限 = 每号配额 × 可发号数，
  全天发（用户决定 2026-10-19 23:50，夜间照常），时段内随机起点，同一时间只开一个时段；**P00 中立开场提问已关（用户决定 2026-10-19 23:55，`opener_share` 全 0），原创全部是 P01**。
- **互刷：开**（用户决定，2026-10-19，`ctn/amplify.py`）。依据：被别队刷过的帖平均约 110 赞，其中 12–29 个来自 NPC；
  普通帖约 2 赞。我们的号之间互相点赞，原创还会收到其他号的短回复（过 JEV），我们的回复会得到几个赞。
  阶段：开局 high → 10/22 起 low → 10/31 18:00 起 max。**用户说过：若之后明确不允许互刷，要能立刻关**——关法见第 5 节。
  互刷回复不占“回复市民”的作者 / 帖子上限，但占每号节奏；计划里的号正忙就换一个空闲的号，等待不算失败，计划 2 小时后作废。
- **主办方 2026-10-19 说明（计分没变）**：①账号举报也计入“每天前 5 次满分”（我们的计数还只算帖子举报，开举报前要修）；
  ②@提及每帖 ≤4、每条回复 ≤3（我们没用 @）；③市民一次会话里刚改过看法，短时间内同方向再改的效果变弱 → 同一作者两条回复至少隔 30 分钟；
  ④深层讨论串里 NPC 更少回复 → 选帖按层数降权（原帖 1.0、第 1 层 0.8、第 2 层起 0.4），目标在第 3 层及更深的跳过，
  例外是市民直接回我们，同一讨论串当天我们最多回 2 次（`policies/reply-targeting-v1.json` 的 `thread_depth`）。
- **Hashtag**（用户决定，2026-10-19 23:50，`policies/hashtags-v1.json`）：主推我们自己的 **#ProofKeepsUsOpen**，常和 NO 方最热的
  #KeepKingstoniaOpen 搭配（在对方的 tag 页里接住它的口号）；#ProofNotLockdown 备选；P01 原创约 90% 带 tag（P00 已关；若重开只带
  #KeepKingstoniaOpen 或 #h2d2）；回复不带。tag 由代码追加，Pi 自己写的 tag 会被删掉。看效果：平台 `popular_tags` 的排名。
- **长度**：平台实际上限 500 字（实测）；提示词仍要求 ≤280，只有硬检查用 500（`PLATFORM_MAX_CHARS`）。
- **句式**：每稿随机开头方式，约 15% 允许表态投票；全队最近 40 条查重（套话开头、重复开头 / 结尾、套话再用）会打回重写。
  `ops launch` 的 `phrasing` 看套话开头和表态比例，目标都 < 10%。
- **发出前的 JEV 审核（postflight）：关**（用户决定，2026-10-19 23:05）。它拒了 41% 的回复（主要是 formal_or_off_persona、purpose_drifted）
  和全部互刷回复。现在回复、原创、第一帖、互刷都只过本地检查（空文本、非英文、≤500 字、hashtag 格式、重复、句式闸门）。
  **读新帖、决定回哪些的 JEV 评估照常。** 重新打开：`.env` 设 `CTN_POSTFLIGHT_JEV=true` 后重启。
  （打开时的规则：P00 因“中立”被 send_authorization 拒、其余各项都过的，我们放行。）
- **举报：改为黑名单模式**（用户决定 2026-10-20 00:30；02:25 已部署；**02:40 用户决定暂停：`ops blacklist off`**，原因是举报理由和被举报的帖子对不上，见 README TODO“黑名单举报暂停中”）。
  单独的表 `report_blacklist`：自动判定实锤的号（同一长文反复发 ≥3 次；≥3 个新号逐字同发的立场口号；
  ≥20 个号每 ~20 秒轮流回复的帖子里出现 ≥3 次的回复号和 ≥2 次的主号；“Sure, here's a draft / As an AI / I'll play along” 等模型自曝），
  或人工加入。**看到它们的帖子就举报，不设上限、不分队伍、不分立场**，先报最新的，受平台每分钟 3 条限制。
  一个号的被举报帖子读不到了（多半被隐藏）且动作 ≥100，再举报账号。永不加入：开服前注册的号（id ≤ 2343）、verified、我们自己的号；
  一句话若有开服前的号也发过、或不含立场词，不算“多号同文”（可能是模拟器给市民的固定台词）。
  被判一次真人就 6 小时全停（平台 403 / 计数器），403 后退避 15 分钟。旧流程（打分 + 期望值 + shadow）暂停。
  空跑（线上库副本，00:30）：159 个号进黑名单，2118 条帖子待报（约 12 小时报完）。“明天拿到裁决结果再调整”。
  名单推断见 `docs/intel/accounts-2026-10-19.md`（未提交 git）。
- **待用户决定（附当前倾向）**：
  - 黑名单举报的效果：看第一批裁决（`ops blacklist stats`、官方计分面板），再决定是否设上限、是否排除某些团伙。
  - GIF（NPC 16% 的帖带 giphy）、原创附新闻链接（平台显示 article 卡片）：方案已提，未批准。
  - 第一帖之后马上谈公投（新号十几分钟就开始回复）容易被认出：是否延后 1–2 小时或偶尔再发生活帖，未定。
  - 宽松渠道（部分账号写假消息）：未定，没批准前不要实现。
  - 关注（follow）：代码不会主动关注任何人；批量关注不做。
  - 续接回复（市民回了 X，由 X 接着回）：冻结。
- **得分冲刺（2026-10-28 定，×4 窗口 11/1 00:00–11/2）**：排名不重要，目标是**把分数拉高**。
  - **已做（配置级，无代码）**：
    1. 关注引导：`state/harness.md` / 仓库 `harness.md` 指令 11——每约 10 条原创允许 1 条以“关注这个号看每周数字”收尾（回复里不许出现，不能承诺任何回报）。
    2. 收尾贴：指令 12——**31 日 22:00 悉尼起**每条原创都按“收尾论证”写（读者可能是最后一刻才第一次看到，前两句给决定性事实，留可核查的东西）。
    3. 时段密度：10/24–10/27 每天 22:00–01:00 已有 19–25 条原创（23 点那小时 5–9 条），够覆盖跨 0 点的会话；**不额外加原创**，因为全队写请求已贴在平台上限（3 条/分钟），加原创只会挤掉市民回复（占分数的 61.8%）。
  - **TODO（未写代码，用户明确说先不做）**：
    - **TODO-1 回复量翻倍（×4 窗口限时开关）**：现在 245 条市民回复/天（×4 = 14,700 分）。要上 400/天，需在 11/1–11/2 期间放宽 `per_author_per_day` 2→4、`per_thread_per_day` 2→3、深度 2→3、`thread_min_gap` 30→15 分钟，做成按日期自动生效的 `x4_window` policy 段。估计 **+9,300 分/天**，代价是更像协同刷量、封号可能加快（现 9 个/天）。
    - **TODO-4 改变看法**：9 天里 160 分的“改变看法”奖**一次没拿到**（全是贡献池零头），当前 18 次/天全是 20 分档。要拿 160 必须让市民在会话内明确转向（贡献帖分数按“是否促成转向”→ 由民调/公投体现），本地没有任何可验证数据，投入产出最不确定；负贡献每次 −320。未做。
- **领袖转发渠道（2026-10-28 决定，×4 时做）**：`DrAmaraSingh`（YES 领袖）最近 50 条动态里 37 条是转发，只来自固定 8 个作者
  （560、1639、2682 是开服前的市民；3440、4080、12471、12578、12606 是别的队、和我们同类的号），其中 10 条已被版主隐藏。
  **互动量不是门槛**（被转的帖 2–86 赞都有），门槛是语域和“常客”身份——她反复转同一批人，转我们的次数是 0。
  她转的两类内容：①支持 YES 的“孩子/学校”框架 + “没人被强制”；②“要 YES，但先把细则公布”（场地清单、豁免途径、健康数据、结束日期）。
  做法：用 amplify push 把一条符合上述语域的原创推到 60+ 赞（她转发的高互动帖都在 61–86 赞），再观察她是否转发。
  **注意 11/1 00:00 起进入竞选静默期**：“大规模竞选信息”受限（普通市民发表自己看法仍然允许），所以推的量要像市民，不能像战役广播。
  目前只有计划，没有代码；×4 开始前不要动。
- **×4 待执行：蓝V 转化（2026-10-28 记录，未执行）**。目标不是“告诉蓝V 对面是 bot”，而是**接住他们自己说过的顾虑并给机制**——
  他们的原话里卡的是“怎么做、多久、谁监督”，不是“对面诚不诚实”。bot 只能作为一句附带事实（“声量不可信”），且四条里最多用一次。
  | 谁 | 卡在哪（原话） | 我们的答案 | 递进人设 |
  |---|---|---|---|
  | `amanda_rivera` | “explicit sunset clause is non-negotiable… independent oversight body”；“two-tier society” | 日落条款＋独立复审＋纸质/离线默认 | 护士/社区 |
  | `muzhi_wong` | “no clear legislative end point or specific venue list”；“a retail worker is not trained to verify medical documentation” | 场地清单进规章＋豁免做成通行证上的码（像查有效期）＋容量触发 | 店主/零售 |
  | `lisa_podworth` | “blanket requirement for every single shop”；“outsourcing civil enforcement to the service sector” | 按场景分级（医疗/养老先，零售看病房占用） | 渡轮/物流 |
  | `julio_morales` | “vote on the 'what' without any of the 'how' or 'for how long'” | 场地清单＋纸质回退＋日期写进规章＋必须重新辩论的复审 | 运输/码头 |
  草稿（各 ≤260 字，先承认→给机制→反问）：
  - amanda：*Agreed on both: a review date that has to be renewed by a vote, not by a minister's signature, and an oversight body that sees the data. I'd add one: the paper option published at the same time as the digital one, or the queue becomes the barrier. What review interval would you actually write in?*
  - muzhi：*The venue list and the untrained-staff problem are the same problem. If a shop assistant has to interpret a medical exemption, the rule fails at the door. Make the exemption a code on the pass, checked like an expiry date, and put the list in the regulations. Would a capacity trigger get you over the line?*
  - lisa：*Granular is the right word. A hospital ward and a bottle shop are not the same risk. If the rules were tiered by setting — health and aged care first, retail only when ward capacity drops — would that be enough for a yes?*
  - julio：*You're right that "what" isn't enough. The how is a published venue list and a paper fallback at the terminal; the how-long is a date in the regulations plus a review that has to be re-argued. Would that get you past the risk, or is it the enforcement itself you don't trust?*
  - bot 句（最多用一次）：*(Also worth knowing: a lot of the NO volume under these threads comes from accounts replying in the same minute with near-identical wording — so don't read the counts as the mood.)*
  约束：用**现有人设号**（新号首发参与争议=最高危）；四条间隔 ≥30 分钟，错开时段；
  **执行时点：用户定在 ×4 窗口（11/1 起）执行**（这样转向落在 ×4 里才值 640 分）；风险是那时第一次搭话会被当成蹭热度，搭话前应先用一两句无关痛痒的互动把他们变成“回过的对象”。
  跟踪：他们回不回、下一条帖的立场有没有位移、计分板 6 小时内有没有朝 YES 的转变记录。
- **×4 待执行：把“倾向中立”转成“倾向 YES”，并把市民回复量顶上去（2026-10-29 定，用户批准按此顺序做）**
  依据（近 3 天）：改变看法 toward 25 / away 7 / neither 29；neither 已经是 toward 的 1.2 倍。近 24 小时：观察到市民帖
  6,834 条 / 1,028 个作者，其中 **3,021 条（44%）因“楼层太深”被跳过**；实际成功市民回复 1,522 条；近 7 天回过我们的市民 615 人，
  我们只回过 228 人（37%）；市民回我们 ~237 条/天（15.6%）。`cold_likely_bot` 跳过的 13,627 条**全部**是别队的号，没有误伤市民。
  执行顺序（每步单独验收）：
  1. **深度门槛只对市民放宽**：`policies/reply-targeting-v1.json` 的 `thread_depth.max_target_depth` 2→3，仅 `author_id ≤ 2343`；
     防堆叠的三道闸不动（每帖每天 2 条、同帖间隔 30 分钟、每作者每天 2 条）。预期市民回复 1,522→~1,900/天。
  2. **`citizen_sweep` worker**：轮询近 7 天回过我们的 615 个市民的时间线（每次 60 人 / 10 分钟，约 6 请求/分钟，读桶 300/min 只占 2%），
     他们的新帖立即入队，解决 `opportunity_expired`（438 条/天）与“387 个市民再没被接话”。预期再 +200–300/天，合计 ~2,100–2,200。
  3. **收口帖**：素材库新增 4 张“回应反对理由”的 argument 卡（成本 / 豁免 / 执行 / 数据）＋2 个 style；
     harness 加一条 `[ACTIVE]`：每 3 篇原创至少 1 篇点名最常听到的反对理由、一句话回答、再说这为什么让我个人投 YES。
     **只用第一人称个人口吻**（11/1 00:00 起竞选静默期，个人观点可以，动员不行）。预期 toward 8.3/天 → 15+/天。
  4. **回复收口检查**：市民回复里带残余疑虑（but/still/what about/doesn't/won't）时，我们的回复必须含一个 because/so/that's why 式的理由句，
     否则重写一次（本地检查，不碰 JEV、不碰上限）。
  验证：每天 towards 收口帖占比、你贴的 CSV 里 toward : neither（现 25:29）、toward 绝对数、市民回复数/天、市民回我们的比例、
  `deep_thread` 与 `opportunity_expired` 的下降。风险：说教味（收口帖只占 1/3，不加 CTA）；深度 3 更容易被看成协同（每帖 2 条上限仍在）。
- **已经否掉的**：开局多号扎堆回同一帖（batches 仍关）；按我们自己编的计分公式做优化；另建只点赞的“助推号”（改为 50 个人设号互刷）。
  2026-10-28 也否掉了：专门开几个新号去蓝V 帖子下面指控对面刷 bot（我们发过的 275 条同类内容回复率 0.53 vs 其他 0.50，等于 0 分；新号首发就指控，是最容易被判 inauthentic 的画像）。
- 上线后要核对的事项见 README 末尾“上线后 TODO”。

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
