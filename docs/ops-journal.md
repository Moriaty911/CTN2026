# 值班日志

新条目加在最上面。格式和规则见 `CLAUDE.md` 第 9 节。不要写入 key、密码或 token。

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
