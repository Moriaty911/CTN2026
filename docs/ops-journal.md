# 值班日志

新条目加在最上面。格式和规则见 `CLAUDE.md` 第 9 节。不要写入 key、密码或 token。

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
