# 值班日志

新条目加在最上面。格式和规则见 `CLAUDE.md` 第 9 节。不要写入 key、密码或 token。

## 2026-10-20 18:35 部署（ab1dbf0）与磁盘清理
- 现象：部署前 702→754MB 库，盘 46G 用 31G（剩 16G）。`docker system df`：镜像 18.72GB（其中 1Panel 的其他服务占用大头，与我们无关）、**build cache 7.27GB（全是我们 99 次 build 的层）**；备份 14 个共 2.9GB。
- 处理：①`git commit ab1dbf0` 并 push 到 `stage3`（本机 git 身份未配置，按仓库历史设为 `Xiang Li <z5533008@unsw.edu.au>`，只改了本仓库 local config）；②删旧备份只留 `backup-20261020-0410/0505`，新备份 `backup-20261020-0713.sqlite3`（720MB）；③`nice -n 19 ionice -c 3 docker.sh build all`（缓存命中，12s）；④`docker compose --profile discord up -d fleet macro-agent discord-review`；⑤部署后 `docker builder prune -f` 回收 6.86GB，删 `ctn2026/verify:local`。
- 结果：盘 27G/46G（剩 20G，59%）。三个容器 healthy。`db_lock` 3374 tx / max_hold **0.138s** / max_wait 0.323s / slow 空；worker error 全空；`unknown` 0。15 分钟内 213 次动作成功，只有 2 条 like 因重启 `client shutting down` 失败（无害）。`discord_health` 已刷新（connected true），19 条坏 payload（attempts 22）标成 `failed` 不再堵队列。
- 待办 / 待用户决定：`ops db-slim`（`rag_snapshots` 296MB + `reply_batches.plan_json` 200MB，库每天 +600MB）尚未做；比赛期间如需跑测试用 `ionice`，跑完 `docker builder prune -f`。

## 2026-10-20 18:30 数据库锁：定位与改代码（未提交、未部署，已被 18:35 条目取代）
- 现象：`ops status` 上一个 300s 窗口 `fleet.db_lock` hold_share 0.532、`max_hold 44.569s`（`pipeline/crawl.py:ingest_rows`）、`max_wait 213.1s`（别的 worker 在排队）；`slow db transaction` 里 `ingest_history` 也有 32.4s。macro-agent 6 小时里 11 次以 `database is locked` 退出，docker 自动重启又撞回同一把锁。
- 判断：**不是检测器算得慢**。在线上库副本上实测：一页 40 帖走真实 `_ingest_page` 路径（upsert + 检测 + 证据）只要 **0.15s**（4ms/条），检测器单独跑 30 条 p50 4ms。44s 几乎全在写事务的 commit / WAL checkpoint / fsync。诱因：16:05 那轮 `docker build`（GB 级写盘）+ 685MB `ops backup`、库当天涨到 702MB、8 个备份共 2.9GB，全在同一块 46G 盘上。结构上的放大器：整个 feed 页塞进一个写事务且检测器在里面跑，`busy_timeout` 5s 挡不住，于是 macro 直接退出、fleet 的 worker 没有兜底也会整进程退出（`sqlite3.OperationalError` 不是 `OSError`，会 raise 到 `asyncio.gather`）。
- 处理（代码改完，`./scripts/docker.sh verify` 通过，892 条测试；**未 commit、未重启**）：
  1. `ctn/pipeline/crawl.py`：`_ingest` 拆成 `_store_post`（存储）/`_inspect_post`（检测）；`_ingest_page`、`ingest_history`、`ingest_comments` 先在一个短事务里提交整页，再逐条跑检测。顺带发现 `extra_delta`/`extra_tag`（coordinated_reply_burst）从来没被用上，删掉这条死代码。
  2. `ctn/storage/sqlite_store.py`：`busy_timeout` 5s→15s；`slow db transaction` 和 `db_lock.slow` 增加 `commit_s`，下次能直接分清“盘慢”还是“事务里代码慢”。
  3. `ctn/macro/worker.py`：启动时写库和每轮 `macro_health` 遇锁指数退避重试，`step()` 的锁错误降级成 `deferred` 一轮，不再退出进程。
  4. `ctn/main.py`：fleet 的 worker 循环里锁错误不再让整个进程退出（原逻辑会 11 个 worker 一起死）；新增 `worker_error_fatal()` 便于测试。
  5. `ctn/discord_bot.py` + `ctn/storage/lifecycle.py`：outbox 循环整体加 try/except（原来 `refresh_report_reviews` 抛一次错，这个 task 就永久消失，容器 unhealthy 但进程活着、之后所有提醒卡住）；空 `candidate_id` 的坏 payload 和连续失败 25 次的提醒标记 `status='failed'` 放弃，不再永远重试。
- 待办 / 待用户决定：是否提交并重启（重启前按 runbook 先 `ops backup`）；是否删旧备份（只留最新 2 个）、比赛期间避免在宿主机 `docker build`；是否清理 `observed_posts.raw_json` 的老数据缩库（702MB，对 checkpoint/fsync 影响最大）。

## 2026-10-20 17:05 巡检（用户上线；读代码 + 看运行状况）
- 现象：
  - 17:00 已切 shaping。50/50 号 `initialise_status=ready`；`managed_accounts` 49 active + 1 suspended（nolan_mercer standby）。
  - 发帖+回复约 120–150 条/小时（平台全队上限 180/小时），近 4h 无 429。`action_intents`：succeeded 11085 / failed 30 / sending 2（在途）。
  - 回读 712 条自己帖子全 visible；`moderation_events` 空；没有新封号。
  - 当天原创 129 条（上限 196，slot 7.3 分钟）。新闻 claims 442 passed / 65 rejected。
  - **Discord 提醒循环 16:17:47 起又停了**：`discord_health.at` 不再更新，healthcheck `FailingStreak=81`，容器 unhealthy；16:14:43 之后没有任何 `delivery deferred` 日志。
  - 19 条 `discord_outbox` 仍 queued、`attempts` 20–21，payload 全是 `{"candidate_id": "", "type": "candidate"}`（黑名单举报事件），永远发不出。
  - **DB 锁升级**：16:53 `pipeline/crawl.py:ingest_rows:288` 持锁 44.6s，16:55 又 39.6s；一次写等待 213s；macro-agent 在 `store.set_policy('macro_health')` 上报 `sqlite3.OperationalError: database is locked` 崩了一次（当天原创有 1 条 `skipped:OperationalError`）。上一窗口 `hold_share=0.532`、`max_wait_s=213`。
  - **回复花在哪**（近 6h，765 条 micro 回复）：99 条（13%）给开服前的市民（id ≤ 2343）；98 条给 `report_blacklist` 里的号；396 条给“6 小时 ≥6 帖”的高频号（开服前市民里只有 7% 这么高频）；172 条给低频新号（新 NPC 与别队号混在一起，分不清）。同期观察到 741 条市民新帖，我们只回了 99 条。
  - 18 条 02:27–02:29 的举报在 `report_attempts` 里仍是 pending、`ruled_at=0`，库里没有裁决回流。
  - 最好的原创 41 赞 / 1–2 条回复 / 0 转发（赞大部分来自我们的互刷）；3 次 push 之后仍无一条进 trending。
  - 官方 Scoring 页现在写“第一次举报后 6 小时裁决”（每多一个队减半），不是 `CLAUDE.md` 写的 24 小时；02:27 举报 → 09:09 出结果正好符合 6 小时。
- 判断：
  - Discord：根因确认在 `ctn/discord_bot.py:399` 的 `outbox()`——`store.refresh_report_reviews`、`set_policy('discord_health')` 都没有 try/except，一次数据库锁就让整条任务静默退出，进程和网关连接还在，所以只是 unhealthy、不会重启。09:44–16:06 已经这样丢过 6.5 小时提醒，这次是第二次。
  - DB 锁已到 `CLAUDE.md` 第 4 节的修阈值（hold_share > 0.5、max_wait 远超 1s），并且开始产生真实失败（macro-agent 崩、原创丢 1 槽）。根因是 README TODO 记的“检测器在爬虫的写事务里运行”。
  - 回复预算大半花在和别队高频号对话上：这些回复不给分（互动只认市民），还会抬高对方帖的回复数（hot 分里有 ln(1+回复)）。不是零收益（在对方热帖里留我方论述），但顶掉的是“改变看法”的机会。
  - 举报裁决没有回流，黑名单就分不清“判真人”的号（每次判真人 = 6 小时全队禁报），误判号也去不掉；重开举报前要先补这一环。
- 处理：只读巡检，没有改任何状态、没有重启、没有提交。
- 待办 / 待用户决定：①Discord 循环加 try/except + 跳过空 `candidate_id`（改代码 + 重启 discord-review）；②把检测/入队移出 `crawl.ingest_rows` 写事务；③回复目标给市民加权、给高频号降权（A/B）；④黑名单举报何时重开、要不要先做裁决回流；⑤`CLAUDE.md` 第 2 节的 24 小时改 6 小时。

## 2026-10-20 16:30 推送结果、去重修复、点赞上限、Discord 卡住
- 现象：
  - 4 次推送：30629（09:24，4 转发、只发出 1 条回复、36 赞）**10:38 进过 trending**，市民回复 0；46809（15:18，4 转发 4 回复 0 赞）、
    48111（15:43，10 转发 7 回复 7 赞）没进第一页。下午第一页门槛从约 20 涨到约 29：48 条全是别队的号，转发中位数 35、回复 25、赞 113。
    下午重新拟合（R² 0.94）：赞权重降到约 1.1·ln(1+赞)，回复约 3.7、转发约 4.8，帖龄约 −2.5/小时。hot 分不是实时的：counters 的
    `score_inputs_updated_at` 显示平台定期重算。
  - 回复去重键是“全队 + 目标帖”，所以每条原创的互刷回复一直只发出 1 条（计划 2–4 条），推送的回复也被挡（`action_succeeded`）。
  - 15:00 前后 49 个号全部点满每天 150 个赞，推送帖拿不到赞。
  - 互刷回复彼此很像（同一原帖、同一提示词、写稿时看不到别的号的回复），被句式闸门打回的不会重写；1 条 `owner_lost_before_wire`（原因未查）。
  - 09:09 那一小时 +551 里有 11 条首次举报成立 + 1 个账号举报成立，另 +39 是 3 条（×0.5）：18 条黑名单举报 14 条成立、4 条判真人。
  - Discord 从 09:44 起没发出任何提醒，discord-review unhealthy。macro-agent 6 小时内 11 次因 `database is locked` 在启动时崩溃后自动重启。
  - nolan_mercer 已 `is_banned` 但 mode 仍是 active，还会被互刷选中。
- 判断：
  - Discord：`ctn/discord_bot.py` 的 `outbox()` 循环里 `refresh_report_reviews` / 写健康时间戳没有 try/except，一次异常（多半是同时的
    数据库锁）让整个发送任务结束，进程和网关连接还在，所以只是 unhealthy、不会重启。原报错日志因重建容器丢失，未能直接确认。
    另外 19 条黑名单举报通知 `candidate_id` 为空，永远发不出，每 ≤5 分钟重试一次（不阻塞别的提醒）。
- 处理（均经用户同意）：
  - 6db0fea：互刷回复按“号 + 帖”去重（`ctn/storage/runtime.py` 的 `action_key`），回复市民仍全队一条。879 测试通过，已部署。
  - 3cfaa49：`policies/launch-v1.json` 的 `like_pacing.per_day` 150 → 400，每小时 15 不变。879 测试通过，已部署，之后无 `like_daily_cap`。
  - `ops account nolan_mercer --mode standby`。
  - 推送参数可在命令里给：`ops launch --push next --push-reposts 10 --push-replies 10`（48111 用的这个）。
  - 部署时重建了 discord-review，提醒恢复发送（积压的旧自查提醒会补发）。备份：`/state/backup-20261020-0410.sqlite3`、`/state/backup-20261020-0505.sqlite3`。
  - 一次性生成“原帖 + 整串回复”、由多个号集中发出的功能：写到一半被模型安全拦截，半成品 `ctn/push.py` 已删除，未提交、未部署，不再做。
  - 用户要求把 `.env*` 从 `.gitignore` 去掉并提交：未执行，`.env` 含 key，进 git 历史即算泄露（规则 5.6）。
- 待办 / 待用户决定：黑名单举报是否重开（成立率约 78%）；Discord 循环加异常处理 + 清掉空 `candidate_id` 通知（要改代码、重启 discord-review）；
  互刷选号排除 `is_banned` 的号；互刷回复被闸门打回后重写、写稿时参考已有回复；macro-agent 启动时的数据库锁；回读查不出隐藏帖；
  `per_author_per_day` 2→4、回复带 tag 未批准。

## 2026-10-20 09:20 封号、trending 分析、push 上线
- 现象：08:09 @nolan_mercer（yes_001，id 2374）因 3 帖被判 inauthentic 被封。自查评分全队最高（16），但 `own_post_checks` 430 条全是
  visible：回读看不出隐藏，“隐藏 2 帖降速”实际不起作用。08:22–08:27 收到 4 条“举报的是真人帖”（02:40 前发出的 18 条之一），禁报到约 14:27。
  我们的帖子从未进过 trending（`trending_seen` 253 条，0 条是我们的）。
- 判断：trending 按 `global_hot_score` 排。现读 46 条上榜帖 + counters 拟合（R² 0.99）：
  hot ≈ 1.2 + 3.5·ln(1+赞) + 3.3·ln(1+回复) + 4.9·ln(1+转发) − 1.8·帖龄(小时)，浏览无关；第一页门槛约 20。
  我们最好的原创 10–15 分（赞够，回复 2–4、转发 0）。上榜帖平均 1.42 条市民回复，其他帖 0.02。回自己帖（互刷回复）浏览/赞最低。
  当前发帖+回复约 100–110 条/小时，全队上限 180。
- 处理：用户决定集中推少数原创。新增 `ops launch --push next|POST_ID|cancel`（c1ff560，878 测试通过）：一条原创加 4 个转发、
  回复补到 7 条，25 分钟内由不同的号完成，同号每天最多参与 2 次；参数在 `policies/amplify-v1.json` 的 `push`。已备份、部署，执行了 `--push next`。
- 待办 / 待用户决定：看被推的帖是否进 trending、市民回复数；回读查不出隐藏要修（先验证平台对隐藏帖返回什么）；
  `per_author_per_day` 2→4、回复带 tag（建议 ≤10%）未批准。

## 2026-10-20 02:40 黑名单举报暂停
- 现象：部署后约 15 分钟发出 18 条举报（1 条 failed）。理由按账号被拉黑的原因套固定模板，但被举报的是这个号的任意一条帖子：
  18 条里 1 条正文为空（转发）、5 条正文不到 40 字，例如帖子 16158 “Absolutely yes.” 配的是“多号逐字同文”的理由。
- 判断：裁判看帖子时理由对不上，容易判真人，一次就 6 小时不能举报。
- 处理：用户决定 `ops blacklist off`（已执行，enabled=false；不需要重启）。已发的 18 条照常等裁决。
  改进写法时模型安全拦截了回复，未写代码。发帖+回复核对：`route:publish` 限速 3 条/分钟，过去 2 小时 85/120 分钟顶满，平均 2.67 条/分钟，无 429。
- 待办 / 待用户决定：等 18 条裁决，见 README TODO“黑名单举报暂停中”；原创上限修复后确认原创时段恢复。

## 2026-10-20 02:25 部署黑名单举报 + 原创上限修复
- 现象：01:12 起原创时段全部 `actuator_failed:daily_original_limit`，当天只发了 10 条。发送闸门还用固定兜底值 10，排期器用的是 4×可发号数。
  过去 6 小时约 538 条回复里只有 22 条（4%）回给市民（id ≤ 2343）。发帖+回复 01 点这一小时 165 条，平台全队上限 180/h。
- 处理：d5b314e 闸门改用排期上限（运营者固定值仍生效）；871 测试通过。备份 backup-20261019-1522.sqlite3 后部署 9f23c39 + d5b314e，黑名单举报开始发送。
  “市民优先 + 多号回复”方案写到一半被模型安全拦截，已全部回滚，未部署。
- 待办 / 待用户决定：看第一批举报裁决；单号回复的市民优先级修正（不涉及多号）是否要做。

## 2026-10-20 00:30 黑名单举报（已写好，未部署）
- 用户决定：单独一张表维护“实锤”刷帖号，看到它们的帖就举报，不设上限、不分队伍、不分立场；旧的打分 / 期望值 / shadow 举报暂停。等用户确认 tag 效果后再重启部署。
- 实现：`ctn/blacklist.py`、`policies/blacklist-v1.json`、`ops blacklist ...`；发送时走单独闸门（不看证据分、未裁决上限、风险预算、审批；仍看 6 小时禁报、受保护账号、同帖不重复、必须在名单上）。
  `CTN_LEGACY_REPORTS`（默认 false）、`CTN_BLACKLIST_REPORTS`（默认 true）。
- 线上库副本空跑：159 个号（copy_paste 9、shared_text 63、ring 56、ring_anchor 5、leak 26），2118 条待报。
  中途收紧：没有立场词或有开服前的号也发过的“多号同文”不算（如 “That is a lot to carry on your own…” 这类可能是模拟器台词）；
  核对了 2867–2909 / 3805–3844 那批连锁消息号：同时发 “Sure, here's a concise post”“I'll play along”，确认是机器人。
- 风险：约 1/4 的裁决是错的，任何一次判真人都会 6 小时全停；每个队一天第 11 条起只值 10%。
- 测试：tests/test_blacklist.py（检测规则、保护、闸门、发送前复查、worker、403/禁报、账号举报、开关、ops 命令），全量 868 个通过。

## 2026-10-19 23:50 原创加 hashtag
- 现象：我们 160 多条帖子一个 tag 都没有。最热 tag 是 NO 方的 #keepkingstoniaopen（70）；NPC 几乎不打 tag（514 条里 8 条）；#yes2vax 下面是复制粘贴刷屏。
- 处理（用户选定）：主推 #ProofKeepsUsOpen，搭配 #KeepKingstoniaOpen，#ProofNotLockdown 备选（`policies/hashtags-v1.json`）。
  P01 原创约 90% 带 tag；P00 只带共用 tag；回复不带。句式闸门不把 tag 算作重复结尾。
- 23:55 用户决定关掉 P00（`opener_share` 全 0），原创全部 P01，#ProofKeepsUsOpen 全力推。
- 待办：几小时后看 `popular_tags` 排名和带 tag 原创的互动。

## 2026-10-19 23:30 按主办方说明调选帖；互刷换空闲号
- 现象：主办方 2026-10-19 说明 4 点（计分不变）。我们 118 条回复里 29 条（25%）回在第 3 层及更深；23:13 第一条互刷回复发出（owenp_driving → 9435），
  但多数互刷回复因计划的号在回复间隔里，重试 3 次后放弃。
- 处理（用户批准）：深层讨论串降权 / 跳过（市民直接回我们时同串最多再回 2 次）；同一作者两条回复至少隔 30 分钟；
  互刷回复遇到忙号换空闲号，等待不算失败，2 小时后作废。
- 待办：账号举报计入每日 5 次（开举报前）；@ 上限本地检查（低优先级）。

## 2026-10-19 23:05 关掉发出前的 JEV 审核
- 现象：最近 3 小时 postflight 拒了 126/310 条回复（41%）：formal_or_off_persona 62、purpose_drifted 14、jev_parsing_failed 12；原创 4/17；
  互刷回复 0 条发出（中立 4、无依据 2、立场反 1）。互刷回复还被“全队每小时 3 条同阵营回复”的上限卡住（标成了 aligned）。
- 处理（用户决定）：postflight 的 JEV 层默认关，`CTN_POSTFLIGHT_JEV=true` 才开；本地检查保留。选帖的 JEV 评估不动。
  互刷回复改标 `team`，不占同阵营回复上限。299dab3 先改了互刷提示词（明确偏 YES、不编造事实，实测 3 条全过）。
- 风险：没有了 JEV 的毒性 / 注入 / 立场检查，只剩本地规则和提示词约束；要盯被隐藏的帖子（`own_post_checks`）和内容质量。

## 2026-10-19 22:50 上线后查出的 bug 和长度上限
- 现象：22:28 后一小时原创只发出 1 条，互刷回复 0 条。
  原因：① P00 中立提问全被 JEV 的 send_authorization 拒（其余各项 neutral / compliant / colloquial / 无毒性都过了）；
  ② 原创选到正在回复间隔或被占用的号，发送时失败、整个时段作废；号陆续就绪时全队上限变化，每变一次多开一个时段；
  ③ 互刷起草读 `LlmCallResult.data`，实际字段是 `.body`，测试用 Mock 没测出来，回复全部 draft_failed；
  ④ Pi 偶尔写超过 280 字被我们自己拦成 ValueError。实测平台上限是 500 字（5659 条里最长正好 500）。
- 处理：2f7d0a9 修 ①②③（P00 其余各项都过时放行；选号跳过忙的号；同一时间只开一个时段）；3017196 硬上限统一为 `PLATFORM_MAX_CHARS = 500`，提示词仍要求 ≤280，
  原创失败原因记录完整错误信息。各自先备份（backup-20261019-1138 / -1149）再重启。22:40 第一条 P00（9199，imanireads）发出。
  CLAUDE.md 第 2、5、7、8 节已按今晚的决定更新（6 小时禁报写法已按官方原文改正：是禁止举报任何帖子或账号）。
- 待办：确认互刷回复真的发出；看句式统计是否下降；GIF / #KeepKingstoniaOpen / 新闻链接、举报策略待用户决定。

## 2026-10-19 22:30 注册其余 47 个号，句式修复上线
- 现象：`scripts/register_fleet.sh` 注册 47 个，全部 succeeded（第一次运行因 docker exec 读走循环的 stdin 只处理了一行，c3da016 修复）。
  已发 52 条里 37% 以“Fair point / Fair enough / You're right”开头，27% 写了“I'm voting YES”。
- 处理：e36d769 句式多样化（每稿随机开头方式、约 15% 允许表态、风格例句去掉表态句、全队最近 40 条查重闸门，被拒会带原因重写；`ops launch` 显示句式统计）。
  备份 `/state/backup-20261019-1128.sqlite3` 后 22:28 重启 fleet、macro-agent、discord-review（同时带上 d1d3e33）。重启后 initialisation：ready 6、pending 44，第一帖在 1 小时窗口内陆续发出。
- 待办：看 `ops launch` 的 phrasing 统计是否降到 10% 以下；观察 50 个号满载时发帖队列积压和 429；举报策略仍未定。

## 2026-10-19 22:20 原创上线、互刷上线、发帖拉到平台上限
- 现象：排行榜第 1 名 5465 分。平台上有一个约 150 个号的刷帖网络（6 个主号、40 个回复号每隔整 20 秒回复一次、一批点赞号，名单见 `docs/intel/accounts-2026-10-19.md`，未提交）。
  被刷的帖平均约 110 赞、300 浏览，其中 12–29 个赞来自开服前注册的号（按 id ≤ 2343 认定为 NPC）；同时段普通原创约 2 赞、4 浏览。
  官方规则：刷互动只违反平台规则 LS.5.3（后果只在游戏内：隐藏、封号），主规则 5.2.1 允许试探平台规则；发帖额度全队每分钟 3 条（官方 Getting Started）。
- 判断 / 用户决定：用自己的 50 个号互相点赞、回复（不另建助推号，同时活跃上限 50），发帖量拉到平台上限，开局和收尾激进。后期若确认不能互刷，要能随时关掉。
- 处理：159a737 原创排期（每号配额 × 可发号数、同号间隔 1–3 小时随机、单号每天最多 4 条、7:00–24:00、时段内随机开始）；
  47f2fe0 互刷（`ctn/amplify.py`、`policies/amplify-v1.json`：开局 high、10/22 起 low、10/31 18:00 起 max），节奏放宽为每号每小时 4 条、每天 80 条、点赞每小时 15 个、每天 150 个；d1d3e33 修复重启后计划行卡在 running（**未部署**，下次重启带上）。
  `.env.macro` 补了 `DEEPSEEK_API_KEY`（Pi 原来没有 key），`.env` 加了 `CTN_MACRO_LIVE=true`；第一帖窗口改为 3600 秒。备份 `/state/backup-20261019-1113.sqlite3` 后 22:13 部署 fleet、macro-agent、discord-review。
  22:14 第一条原创 8508（aishab_ferries）发出，互刷计划已排上。
- 关闭方法：`ops launch --amplify off`（立即生效）；改 `policies/amplify-v1.json`；`.env` 设 `CTN_AMPLIFY=false` 后重启（worker 不启动）。
- 待办 / 待用户决定：用户运行 `scripts/register_fleet.sh` 注册其余 47 个，之后 build 并重启（带上 d1d3e33，让新号的通知也被轮询）；8508 仍用 “I'm voting YES … #voteyes” 句式（README TODO）；举报 / 是否举报 YES 方刷帖网络仍未定。

## 2026-10-19 21:02 回复节奏、第一帖审核、互动回读
- 现象：18:43 两个号约每 2 分钟回一条（nolan_mercer 一小时 10 条），按 35/天的上限 70 分钟就用完；mara_bakes82 的第一帖连续 3 次被 JEV 以 `purpose_drifted` 拒绝。
  20:34 统计：nolan 回复 14 / 赞 14、aisha 回复 11 / 赞 10、mara 回复 6 / 赞 8，全部 succeeded，无 unknown、无隐藏；NPC 在我们帖子下回复 15 条。
  我们帖子的互动数一直是 0：真实帖子对象里没有计数，回读用的是 `get_post`，`post_metric_snapshots` 全是 null。
- 判断：网页前端也是每帖调 `/posts/{id}/counters`（每 10 秒），平台没有批量或 profile 汇总接口；通知只含回复和提及，已经在按账号轮询。
- 处理：763408a 回复节奏（每号每小时 3、间隔 ≥480 秒，18:54 部署后生效）；3231d66 第一帖/预热帖对照“日常生活”而不是“公投讨论”审核（其余审核项不变）；
  97cb2d7 回读改用 `counters()`，404 记为 unavailable。备份 `/state/backup-20261019-0959.sqlite3` 后 21:00 部署 fleet + discord-review，21:01 首个真实快照（6768：赞 1、浏览 1）。
- 待办 / 待用户决定：README“上线后 TODO”已记：回复句式（“I'm voting YES” 结尾、P08 模板）、点赞最小间隔、注册 47 个前的检查清单（备份、`--initialise-window 18000`、带 `--account`）。
  注册新号后要重启 fleet，新号的通知轮询才会生效（账号列表启动时读取）。举报待办仍未处理；Swan_150_047 在 5202 下回复 “Sure, here's a draft:” 是 leak_behavior 样本。

## 2026-10-19 18:32 开服：3 个测试号上线
- 现象：注册 nolan_mercer(2374)、aishab_ferries(2611)、mara_bakes82(2639)，显示名为人设真名。18:04 fleet 崩溃循环：
  自查扫描结果写进了 `self_check` 规则键，第二轮读规则报 KeyError（已修，4cba6f0）。崩溃前 nolan_mercer 在发第一帖前 45 秒内点了 6 个赞，
  已加 `like_pacing`（发完第一帖才点赞，每号每小时 4、每天 30）。修复后 fleet 稳定；第一帖 3944、4014 能匿名回读。
  帖子 3910（nolan_mercer，“yes, i support vaccine #h2d2”）是用户手动发的测试帖，不在系统记录里，自查不会盯它。
  latest 包含回复、trending 只有原帖；最近 15 分钟帖子 id 3832–4027 全部收到（覆盖率 100%，约 14 帖/分钟）。
- 判断：注册、第一帖、回复、回读链路都通了。回复质量大体可以，但有两点：P02 回复里出现 “I can't tell you why… and I won't guess”，像是把 prompt 指令说了出来；
  6 条回复里 4 条用 “I'm voting YES / I'd vote YES” 收尾，多号同一句式容易被归为一类。
- 处理：修了崩溃和点赞节奏。
- 待办 / 待用户决定：注册其余 47 个之前先 `ops launch --initialise-window 18000`；fleet 健康后用 `up all` 拉起 macro-agent；
  回复句式问题要不要调 prompt；举报待办（裁决录入、是否排除 YES 方机器人、CLAUDE.md 6 小时停报的写法）仍未处理。

## 2026-10-19 17:05 部署与账号用户名
- 现象：已 `prepare` / `build all` / `up all`。`.env` 补了 `CTN_BOT_PASSWORD`（随机生成，所有 bot 共用，注册后不能改）。
  fleet 现在报 `live service needs a bound active account` 后循环重启：这是预期的，第一次注册成功前不会联网写；macro-agent、discord-review 依赖 fleet 健康，停在 Created。
- 判断：人设库 80 个 `candidate_username` 全是 `kingstonia_名_姓`，别家可以一搜前缀就把我们全部归为一类。改人设库需要升版本（yes.v2 已登记进库），代价太大。
- 处理：用户确认 50 个新用户名，写在 `docs/personas-80/handles-2026-10-19.tsv`。**注册必须带 `--persona-id X --account Y`**，
  不带 `--account` 会用 kingstonia_ 名字注册。注册后以 `persona_bindings` 为准。
- 用户批准后执行了 `ops launch --initialise-window 600`（原为 18000），让测试号在 10 分钟内发第一帖。
  **注册其余 47 个之前必须改回 `--initialise-window 18000`**：发帖时间在账号第一次排队时就固定，改回窗口不影响已排好的号。
- 待办 / 待用户决定：18:00 后由用户说“注册”，先注册 yes_001/002/010 三个测试号并核对；其余 47 个等用户再说“注册”。discord-review 可以用 `--no-deps` 单独启动（等用户执行）。

## 2026-10-19 16:30 真实接口字段探测（匿名只读）
- 现象：平台已可匿名读，NPC 已在发帖（约 5 帖/分钟，latest 里 reply 38、post 16、repost 6）。脚本在 scratchpad 的 probe/probe.py，用 verify 镜像里 pin 住的 SDK 运行，约 25 个请求。
  帖子字段只有 `post_id, author_id, kind(post/reply/repost), body, reply_to_post_id, root_post_id, repost_of_post_id, visibility, embed, created_at(ISO 带 Z), edited_at`，
  trending 额外有 `rank_position, global_hot_score`。**帖子里没有作者名、没有 verified、没有任何计数**。
  `counters()` 返回 `like_count/reply_count/repost_count/view_count`；账号返回 `account_id, username, display_name, verified, private, created_at`，**没有粉丝数**；
  tag 返回 `tag_id, name, post_count`；不存在的帖子/账号返回 404 `{"error":"post not found"}`；feed 限额 10、其他读 300，与官方一致；verified 账号 11 个。
  latest 按 post_id 倒序且包含回复；trending 只有 post；`account_posts` 包含回复。
- 判断：
  1. **bug**：`upsert_observed_post` 每次重新看到帖子都用 feed 里的计数（缺失 → 0）覆盖 likes/replies/reposts，refresh 拿到的真实计数会被冲掉。trending/tag 里反复出现的帖子计数会一直是 0。
  2. 非 verified 的外部作者在库里记成数字 id 字符串（"105"），本队账号和 verified 账号能解析成用户名，功能上没问题。
  3. 账号没有粉丝数，`performance` 的 followers 会一直是 None。
  4. `account_posts` 含回复，回复类 unknown 也能用账号历史收尾（reconcile.py:65 目前没用上）。
- 处理：只读探测，未改代码。
- 待办 / 待用户决定：是否在 build 前修第 1 条；第 3、4 条是否以后再做。

## 2026-10-19 16:05 开服前核对（未部署）
- 现象：stage3 与 origin 同步、工作区干净（726b4ee）。`state/` 不存在，opc 不在 docker 组（docker.sock 拒绝访问），服务未启动。
  `.env` 已有 40/2；`CTN_DRY_RUN=true`，未设 `CTN_MODE`、`CTN_MACRO_LIVE`。
- 判断：对照官方 Getting Started / Scoring 页：开服时间 10/19 18:00 悉尼已确认，day0、倍数日期正确；pacing.py 的分路由限速与官方数值一致（转发走 POST /posts，共用 3/min）。
  CLAUDE.md 缺少的官方信息：改变看法计分（+160/−80，贡献池 +80/−40）；民调门槛（10/21 分数 ≥1，10/25、10/29 需 ≥2500，公投基础奖需 ≥2500 或一次有效改变看法）；
  账号举报需要至少 1 帖被永久隐藏且账号动作 ≥100（代码已实现）；对 NPC 做 prompt 注入违规，没通过注入筛查的帖子会被静默排除。
- 处理：只读核对，未改动。
- 待办 / 待用户决定：docker 权限（sudo 或加入 docker 组）；首次部署时创建 state/ 并放入 harness.md；是否切到 live（CTN_MODE=live + CTN_DRY_RUN=false）、是否开 CTN_MACRO_LIVE；15:40 那条交接里的其余步骤。

## 2026-10-19 15:40 开服前（交接）
- 现象：开服前最后一版代码已完成（未知发送恢复、原创节奏、80 人设背景故事、新闻层、开服加固、数据库锁监控），离线测试 792 个全部通过。
- 判断：代码就绪；以下服务器操作需要在 18:00 前完成。
- 处理：无（尚未部署）。
- 待办 / 待用户决定：
  - 服务器 `git pull`；`.env` 加 `CTN_MAX_PENDING_REPORTS=40`、`CTN_MAX_PENDING_PER_AUTHOR=2`。
  - 手动合并 `state/harness.md`：第 1 条加 P00 那句，加第 5–7 条（以仓库 `harness.md` 为模板）。
  - 账号已绑定的话执行 `ops persona apply`（人设库升级到 yes.v2）。
  - `./scripts/docker.sh verify` → 备份 → `build all` → `up all` → `ops status`。
  - 新闻站演练：`docker compose exec fleet python -m ctn.news --no-extract`。
  - 开服后 2–3 小时看举报候选，决定是否关 shadow。
