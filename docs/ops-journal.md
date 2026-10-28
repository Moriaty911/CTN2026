## 2026-10-28 21:05 得分冲刺：关注引导 + 收尾贴（#1 #4 只记录不实现）

- 背景：用户给的判断是"排名无所谓，尽量冲分数"。用官方活动日志把 87,673 分拆开：市民回复 61.8%、点赞 23.7%、浏览 8.4%、关注 4.4%、转发 1.7%，改变看法类只占 3%（9 天里 160 分的"改变看法"奖一次没拿到）。所以拉分靠"市民回复量"，不是论证深度。
- 处理（配置级，不改代码）：
  1. `state/harness.md` 和仓库 `harness.md` 新增指令 11：每约 10 条原创允许 1 条以"关注这个号看每周数字"收尾；回复里不许出现，不能承诺回报。理由：关注 20 分（×4 = 80），而且 Home feed 优先展示已关注账号，是唯一还能放大的渠道；当前只有 17 个/天，且从没主动要过。
  2. 新增指令 12：**31 日 22:00 悉尼起**每条原创按"收尾论证"写（前两句给决定性事实、留可核查的东西）。理由：×4 从 11/1 00:00 开始，市民会话跨过 0 点就按 ×4 计分，10/31 深夜发的内容正好落在窗口里。
  3. `tests/test_harness_integration.py` 加两条断言（新指令必须在模板里）。verify 999 tests OK。
  4. 不额外加原创：全队写请求已经贴在平台上限（3 条/分钟），加原创只会挤掉市民回复（占 61.8%）。10/24–10/27 每天 22:00–01:00 已有 19–25 条原创，边界时段不缺内容。
- 记录（未实现，用户明确说先不做）：CLAUDE.md 第 8 节新增"得分冲刺"段，TODO-1 = ×4 窗口限时放宽回复上限（预计 +9,300 分/天，代价是封号风险）；TODO-4 = 争取 160 分的改变看法（本地无可验证数据，最不划算）。
- 生效方式：macro 每次起草都重读 `state/harness.md`（`ctn/macro/worker.py:164`），**不用重启**。
- 待办：观察 11 和 12 是否真的落到稿子上（查原创里 follow 出现的比例、以及 11/1 起的收尾风格）。

## 2026-10-28 09:50 巡检 + 补 1 个号（grace_ollerenshaw）

- 现象：巡检时发现 `ravi_almeida_nh`（healthcare_support）在 09:24:55 被封，active 掉到 49，且它的 `account_states.mode` 又是 active（第三次同样的残留）。
- 处理：
  1. `ops account ravi_almeida_nh --mode standby`。
  2. 注册 `grace_ollerenshaw`（人设 yes_117，healthcare_support 组，平台 id 14505），`CTN_BOT_ACCOUNTS=95`。
  3. 同样临时把 `initialise_spread.window_seconds` 设 900，让它插队发首帖，随后恢复 3600。注意：绑定的生效不需要重启（`warmup_step` 每次 `registry.attach(store)`），新号在旧容器里就发了首帖（09:26:25，post 703093）。
- 验证：active 50 / suspended 37，`account_states` 全部对齐（active 50 / standby 37）；`persona_catalog.match=true`；worker 无 error；**最近 60 分钟 50 个 active 号全都有动作，零静默**；近 4 小时每小时 4-8 帖、73-86 回复、283-437 赞（09 点为止 3 帖 51 回复 362 赞）。失败仅 2 条，都是良性：`client shutting down`（重启残留）、`post not found`（目标被删）。
- 待办：`account_states.mode` 残留已连续三次出现（nabil_avery、ravi_almeida_nh 等），建议在封号检测时顺手写 standby——目前是人工补；等下次改代码时一起做。

## 2026-10-28 09:15 再补 1 个号（owen_farrow）

- 现象：`nabil_avery`（law_policy）在 10-28 01:00 被封，active 49 / suspended 36；它的 `account_states.mode` 还是 active（同一个残留问题）。
- 处理：
  1. `ops account nabil_avery --mode standby` 对齐状态。
  2. 注册 `owen_farrow`（人设 yes_105，law_policy 组，平台 id 14468），`CTN_BOT_ACCOUNTS=95`（未删除 85）。
  3. 为免首帖再等 40-60 分钟，先把 `initialise_spread.window_seconds` 临时设 900，重启后新号排在 +4 分钟，随后立即恢复 3600。
- 验证：`persona_catalog.match=true`、worker 无 error；首帖 09:00:21 发出（post 701323），`initialise_status='ready'`，随后 1 赞 1 回复均成功。现在 active 50 / suspended 36（全部 standby），15 分钟产出 137 赞 / 4 帖 / 31 回复（约 124 回复/小时）。
- 待办：无。注意 `initialise_spread` 已回到 3600，以后新号仍按哈希偏移散布。

## 2026-10-27 17:55 补 1 个号到 50

- 现象：`managed_accounts` active 49 / suspended 35，差 1 个。另外 `account_states` 里 `lena_armitage` 虽然已 suspended，mode 还停在 active（封号后没有置 standby 的残留）。
- 处理：
  1. `ops account lena_armitage --mode standby`，与状态对齐。
  2. 注册 `ruth_callaghan`（人设 yes_100，law_policy 组，平台 id 14230），用 `CTN_BOT_ACCOUNTS=90` 绕开"未删除账号数"检查（当时未删除 84）。现在 active 50 / suspended 35。
  3. 注册完 `up -d --force-recreate fleet` 重启，让新号的通知轮询和账号列表生效。
- 验证：`ops status` 的 `persona_catalog.match=true`（loaded = active = `d6f95762…`），worker 无 error；重启后 10 分钟 118 赞、3 帖、19 回复（约 114 回复/小时，正常）。新号 `initialise.pending=1`，按 `initialise_spread` 的哈希偏移排在悉尼 18:13 发首帖——**首帖发出去之前它不会回复**（`account_initialise_ready` 是回复的前置条件）。
- 待办：18:15 左右确认 `ruth_callaghan` 首帖发出、`account_lifecycle.initialise_status='ready'`，随后开始回复。监控脚本 `/tmp/ruth-watch.sh` → `/tmp/ruth-watch.log`，每 2 分钟记一次 posts/replies/init 状态。

## 2026-10-27 17:35 停写 6 小时：人设目录失配

- 现象：`publications` 最后一条是悉尼 10:53，之后 6 小时零发帖、零回复、零点赞；`action_intents` 这 6 小时一条都没有。容器全部 healthy、worker 全无 error、`business_stalled` 也没有触发。`/logs/actions.jsonl` 里有 11,159 条 `"detail":"persona_catalog_mismatch","status":"blocked"`。
- 判断：14:05 那次把 100 人设扩到 120 并重建镜像，镜像里的人设目录 hash 是 `d6f957…`，而数据库 `active_persona_catalog` 还指向 100 人版 `2e2f56…`。`ctn/persona/router.py:96` 的闸门要求两者相等，否则拒绝一切带人设的写操作（发帖/回复/点赞/举报）。部署时只重启了容器，漏跑 `ops persona apply`。同类问题 10-26 也发生过一次（停了 3 小时）。
- 处理：`ops persona apply`（active 更新为 `d6f957…`），约 90 秒后写入恢复。随后加了监控：`ctn/persona/router.py` 新增 `catalog_match()`，心跳带 `persona_catalog{loaded,active,match}`，失配时每小时发一次 Discord 告警，`ops status` 的 `fleet` 段也能看到。999 个测试通过，commit `8963a7e`，重建部署后 `match=true`、worker 无 error、5 分钟内 43 赞 12 回复 2 举报。
- 待办：把“部署后确认 `ops status` 的 `persona_catalog.match`”写进 runbook 的部署检查清单；观察今天剩余时段产量是否回到每小时 ~500 分。

## 2026-10-27 14:05 人设池 100 → 120

- 现象：10-27 累计被封 34 个号（75 个注册号），人设池只剩 16 个未绑定。按当天 9 个/天的速度，两天内会用完，届时新号只能复用已封人设（同名同职业再次出现，容易被认出是同一队在补位）。
- 判断：池子要按“每天 3–4 个正常、9 个极端”预留至少 4 天余量，即 30–40 个空闲人设。
- 处理：`config/personas/yes.v1.json` 增加 `yes_101`–`yes_120`，按 11 个职业组补齐（medical +3、law +3、independent +2、其余主要组 +1~2，现为 11–14 人/组）。每个人设都写了完整 `story`（14 个字段）、4 条 `example_posts`（2 daily + 2 referendum）和 4 条日常帖（`config/personas/daily-posts.v1.json`），全部 ASCII、≤280 字、无重复文本、日常帖不含疫苗/公投/选举等话题词。同时：`ops persona validate` 里写死的 `personas: 80` 改成 `len(data['personas'])`；`docs/personas-80/preview.md` 重新生成（120 人）；`tests/test_persona80.py` 的“删 21 个即非法”改成“删到 79 个即非法”（原来假设目录固定 80 人）。
- 验证：`./scripts/docker.sh verify` → verify 镜像 998 测试全通过；`ops persona validate` → `{"valid": true, "personas": 120}`。
- 待办：新号仍需手动绑定+激活；池子现有 36 个空闲人设（120 − 50 active − 34 suspended），按 9 个/天约 4 天。

## 2026-10-27 10:20 降低句式同质化 + 新号限速（被封 34 后）

- 现象：上一个 24 小时被封 34 个号。逐条回读被判 `inauthentic` 的帖（34 个号各取最近 12 条，共 408 条）：**11 条 404（隐藏），分布在 10 个号上**；50 个存活号最近 315 条全部可读。隐藏的都是普通回复，回复对象是普通市民（`bot_registry` 全是 unknown、怀疑分 0–30）。封号通知的原话是"already had a post hidden by the moderator, and it had taken at least 100 actions"——所以路径是**某条帖被版主隐藏 → 该号变成"可举报账号" → 别的队提交账号举报 → 封号**，不是平台自动识别。同一批号的行为指标（动作量 563 vs 434、回复 177 vs 183、对市民 15.3% vs 17.9%、4-gram 重合 0.379 vs 0.366、同帖 1 小时内 ≥3 个号回复 5.1% vs 3.2%）都查不出区别。
- 判断：内容本身不像机器人，最可能被版主按"风格"判——5,877 条回复里 24.4% 含 "I'd want"、3.6% 含 "spelled out"、2.7% 含 "who checks"，开头集中在"the venue list"(117)、"what would the/a"(177)、"I support requiring/the"(170)。这是假设，不是证明；第 1 步（把"被隐藏"变成可观测）用户说先等一等。
- 处理（按用户"先做 2 和 3"）：
  - 2. `policies/phrasing-v1.json` 新增 `overuse`：开头模式配额（`^(the venue list|what would (the|a|an))` ≤6%、`^(i support (requiring|the)|i run (a|music))` ≤8%）和短语配额（`i'd want` ≤18%、`spelled out` ≤6%、`who checks` ≤5%、`what data would be collected` ≤4%、`the venue list` ≤5%），超出即打回重写；`avoid_in_prompt` 把这几条写进提示词；`ctn/phrasing.py` 新增 `overuse_reason()`（由 `repetition_reason()` 末尾调用）、`stats()` 增加 `overused` 分布、`instructions()` 追加"这些说法到处都是，避开"。
  - 3. `policies/launch-v1.json` 新增 `warmup`：首帖后 36 小时内每号每小时 ≤2 条、相邻 ≥900 秒（正常是 4 条 / 300 秒）；`ctn/pipeline/targeting.py` 新增 `pacing_rule()`，`account_pacing_reason()` 改用它。依据是当天测得"没有任何号在首帖后 37.3 小时内被封"（中位 167.7 小时），属于预防，不是实证效果。`ops launch` 输出加 `warmup` 便于查看。
  - 测试：`tests/test_phrasing.py` 新增 `OveruseTests`（6 条）、`tests/test_reply_targeting.py` 新增 `WarmupTests`（4 条）。
- 验证：全量离线 998 测试通过（`docker run --rm -v $PWD:/app ... unittest discover`）。
- 待办 / 待用户决定：
  - 部署（重建 fleet + 重启）要用户点头。
  - 观察 `ops launch` 的 `phrasing.overused` 与 `stock_opening_share`，以及 36 小时内新号的回复量是否真的降到 2 条/小时。
  - 被封原因仍有未知项：谁在报我们、版主按什么判。第 1 步（隐藏帖观测）还在等。

## 2026-10-27 09:35 再封 9 个，补满 50

- 现象：`managed_accounts` active 41 / suspended 34（上一条日志时是 50 / 25）。新封 9 个：`beth_cruz`（10-27 06:04）、`arunblake`（03:45）、`camila_ford`（02:36）、`oscarlinwood`（02:34）、`leonabara`（02:33）、`seth_alvarez`、`selene_designs`、`pete_aranda`、`malik_jensen`。涉及组：retail_food、education_sport、healthcare_support ×2、technology_digital、agriculture_trades、office_community ×2、independent_commentary。封号速度约 **9 个/天**，比 10-24 前后的 3–4 个/天明显加快。
- 判断：封号不再是"老号积累"，新号和低风险号都在被抓；补号只能维持规模，不解决原因（原因仍未查清，见 10-24 的 404 证伪记录）。
- 处理：
  - 补 9 个新号（人设池还有 25 个空位，按被封组的分布挑）：`rosa_kim`（yes_083 retail）、`harriet_dowd`（yes_088 education）、`verity_combes`（yes_090 healthcare）、`esther_wills`（yes_091 healthcare）、`roman_pekar`（yes_092 tech）、`tamsin_ridgeway`（yes_095 agriculture）、`gwen_abbot`（yes_096 office）、`isla_brennan`（yes_097 office）、`zain_porter`（yes_080 independent）。注册用一次性 `CTN_BOT_ACCOUNTS=90`（内部 undeleted 已 84；`.env` 仍是 50）。`rosa_kim` 第一次没返回状态，重跑一次才成功（注册偶发静默失败，值得记）。
  - 9 个被封号设 `mode=standby`；发现 **`rafa_dives`、`lina_moretti` 两个早已被封但 `mode` 还是 `active`**（前几轮补号时漏设），一并改掉，否则 worker 会一直尝试它们。
  - `docker compose --profile discord up -d --force-recreate fleet` 重启两次（第一次在设 standby 之前）。
- 验证：`managed_accounts` 50 active / 34 suspended；`account_states` 50 active / 34 standby；三容器 healthy、worker 无 error、`business_stalled` 无；重启后 5 分钟 10 条回复 + 3 条帖 + 65 个 like；`initialisation.pending=9`（9 个新号在排队发首帖）。人设池剩 16 个。
- 待办 / 待用户决定：
  - 按 9 个/天的封号速度，16 个人设两天用完；要不要现在再扩一批（纯数据改动，`yes_101`–`yes_120`）。
  - 封号原因仍未定位：10-24 已经证伪"隐藏帖 → 404"的假设（4,751 条自己的帖子全部 200）。可查的方向：同 IP/同注册批次、回复文本模式、举报来源（我们被别队举报而不是系统判定）。
  - 上一条日志留的两项还挂着：`backup()` 改用 `VACUUM INTO`（现在 `--slim` 在 9G 空闲时会报 disk full）、`state/backup-20261026-1203.sqlite3` 1.6G 未压缩副本删不删。

## 2026-10-26 14:05 回复转向"可说服的人"；素材库补两条"两级社会"

- 现象：24 小时 1,753 条回复里，目标 **73% 立场相反**，其中 **58% 是别队机器人**；市民只占 21%。按市民细分：摇摆 93/98（95%）、opposing 258/287（90%）、aligned 42/54 都已回过——**提高"摇摆"权重不会有新增**，缺的是"看到多少市民帖"（市民帖只评估了约 20%）。发泄帖 23/287，中位 0 赞，回复后接话 17% vs 21%（样本小）。
- 判断：A（提权）+ D（跳过发泄帖）都是**用现有数据能算出来的小改动**，收益有限但方向正确；真正的天花板在覆盖（B）和"改变看法"的转化（红分），不在排序。E/F 是给 Pi 的语料层调整，成本近乎为零。
- 处理（提交见下）：
  - A `ctn/pipeline/targeting.py`：新增 `_CONCERN`（not against / personal choice / as long as / show us …），`persuadable()` 认"有顾虑的反对"；`weights.persuadable` 8 → 14。
  - D **已撤回**：先实现了 `_VENT` + `venting()`（opposing、无问号、含侮辱词 / ≥2 个感叹号 / ≥3 个全大写词、engagement < 20 → `targeting:venting`），上线后拿最近 24 小时 5,965 条市民帖回放验证：命中 4.6%（276 条），其中 238 条只是用了两个感叹号、36 条是全大写的**正经论据**（如 "A FINITE, PRIVACY-RESPECTING PLAN MAKES SENSE—SET A HARD END"），只有 2 条真的在骂人。和我手工数的"23/287 条是对喷帖"差一个数量级，说明这个代理指标抓的是排版不是情绪。省下的假设收益不值得拿真实对话去换，所以整套撤掉，policy 里的 `hostile` 段也删了。以后要做应该用 JEV 自己打的 `post_type=venting` 标签（`ctn/jev/rubrics/post.v3.json` 里有），但那是评估之后才有，省不了 JEV。
  - 没有改 `brain.py` 的 prompt_type：JEV 的 `response_decision` 里本来就有 P07（Empathy & Concern）和 P08（澄清提问），从 Python 强行改写会和 postflight 的 purpose 检查打架。
  - E `harness.md` + `state/harness.md`：新增第 8–10 条 `[ACTIVE]`（原创只打"强制接种的边界"和"旅游/封关"两条线、回复 4–6/10 以一个问题收尾、"两级社会"只在回复里答）。
  - F `config/rag/yes.v1.1.json`：新增 A022（两级社会是真实担忧，用豁免/过渡回答）、A023（把它变成可检验的问题：谁豁免、怎么申诉、是否落日条款）、S031（P02 风格示例，242 字）；raw_review 的 output_ids 已回填。
- 验证：`./scripts/docker.sh verify`（A/E/F 版本 991 tests OK；撤回 D 后重跑，见下）。
- 未处理 / 待用户决定：
  - C（给别队机器人回复设上限）**用户明确不做**，继续保证回复量。
  - B（市民覆盖 20% → 40%，广谱读取）未做，需要动 feed 段和请求预算。
  - `ops status` 的 `catalog_match`、心跳"30 分钟零发布"告警仍挂着（用户说"等一下"）。
  - `state/backup-20261026-1203.sqlite3`（未压缩的 1.6G 副本，同目录已有 .gz）还在，删不删要用户点头。

## 2026-10-26 13:10 人设目录失配，写流量停了 3 小时（已修）
- 现象：
  - 部署用户 11:50 的改动时发现线上**从 09:34:45（悉尼）起零发布**：`publications` 最后一条 09:34:45，
    `action_intents` 最后一条 09:38:28，到 12:26 空转 2 小时 52 分。容器 healthy，worker 无 error，日志无 429/403。
  - 最近 2 小时 `post_tasks.status='skipped'` 里 `all_accounts_disabled` **1,049 条**。
- 判断：
  - `ctn/persona/router.py:_eligible()` 第一句 `if registry.catalog_hash != store.policy('active_persona_catalog'): raise ValueError('persona_catalog_mismatch')`；
    `ctn/pipeline/brain.py:367` 接住这个 ValueError，把任务记成 `all_accounts_disabled`（名字误导：不是号被禁，是路由拒绝）。
  - 实测两个值：镜像里（100 个人设）`2e2f5634…`；库里 `active_persona_catalog` 是 10/19 16:45 的 `1e1de81b…`（80 个人设）。
    `persona_catalogs` 里 `2e2f5634` 是 09:40:45 补 4 个号时注册的（`INSERT OR IGNORE`），但没有 activate。
  - 损失按 ~170 条写/小时估：约 480 条回复/原创/点赞没发出去，其中约 250 条本该给市民。
- 处理：
  - `docker compose exec fleet python -m ctn.ops persona apply`（= `register_persona_catalog(activate=True)`）把 policy 指向 `2e2f5634`。
    不动人设内容、不动绑定（`persona_revisions` 版本校验通过，幂等）。
  - 验证：12:39:50 第一条 post 成功；之后 10 分钟 8 帖 + 11 回复 + 64 赞 + 1 举报，`all_accounts_disabled` 归零。
- 教训 / 待办：
  - 这类失败**完全静默**：容器 healthy、heartbeat 正常、异常被吞进任务原因，`ops status` 不显示“零发布”。
    建议加两道护栏（未做，需改代码）：①`ops status` 输出 `catalog_match`，失配标红；②心跳里“最近 30 分钟零发布”直接 Discord 告警。
  - 以后 `register_fleet.sh` / 补号之后，必须确认 `active_persona_catalog == registry.catalog_hash`，否则等于把整个系统停掉。

## 2026-10-26 11:50 回复优先给开服前市民，别队机器人回我们不再加分（已部署 12:21）
- 依据（用户在线上跑的只读查询，过去 48 小时 `publications.kind='reply'`，市民 = `author_id <= 2343`）：
  回复市民 887 条，引来市民回应 94 条（10.6%）；回复其他人 3,245 条（79% 的回复），引来市民回应只有 35 条（1.1%），
  但其中 16.6% 被目标本人回了、23.2% 被别队机器人回了。只有市民的互动计分，所以 79% 的回复只换来 27% 的市民回应。
  同时"对方回了我们"在代码里有三个奖励（排序 +15、深串跟进、解锁第二条回复），别队机器人回我们也能拿到，形成只耗写配额的循环。
- 改动：
  1. `ctn/pipeline/targeting.py`：新增 `is_known_citizen()`（id <= `citizens.max_account_id`，永不包括自己的号）、
     `responder_counts()`、`counted_reply_to_us()`。市民的 `bot_probability` = 0；`audience()` 加 `weights.known_citizen`
     （25，和 JEV 分 20–40 的浮动同一量级，否则排不动）；`verified` 改为看档案，不再用 `p_bot == 0` 推断。
  2. 排序加分、深串跟进、`bot_filter` 的豁免、冷帖豁免、第二条回复的解锁，都只认"值得奖励的回应者"：市民一定算；
     其他人只有 bot 先验 < `citizens.responder_max_bot_probability`（0.5）才算——未知新号（0.3）照算，
     suspected/confirmed/一小时 ≥4 帖的不算。`count_newcomers: false` 可切成只认市民。
  3. 检测器分数（`bot_filter.min_suspicion`）不再过滤市民。
  4. `policies/reply-targeting-v1.json`：`weights.known_citizen` 25、新增 `citizens` 段（带依据说明）。
- 测试：`tests/test_reply_targeting.py` 新增 `CitizenTargetingTests` 6 条；离线环境（无 PyPI，httpx/openai/pydantic 用导入空壳）
  改动前后全量各 98 条环境性失败、集合完全相同，定向测试 38 条全过。部署前请照常 `./scripts/docker.sh verify`。
- 观察点（部署后 24 小时）：重跑同一条查询，看回复里市民的占比是否明显上升、"任何市民回"的总数是否上升；
  `post_tasks` 里市民任务的 `expired` 是否减少；若市民候选不够，空出来的写配额会回到其他人身上，不会闲置。
- 回滚：`weights.known_citizen` 设 0、`citizens.responder_max_bot_probability` 设 1.0 即恢复旧排序。

## 2026-10-26 10:45 备份改成"核心表 + gzip"
- 背景：`ops backup` 每次整库复制，审计表（`rag_snapshots` 1.98G、`reply_batches` 1.14G）占 67%，
  备份 4.7G。10/26 09:45 就是因为备份写不下而失败（盘满）。
- 改动：`ctn/storage/sqlite_store.py` 的 `backup(destination, exclude=())` 支持复制后删除指定表并 `VACUUM`；
  新增 `gzip_backup()`（压缩后完整解压校验一次，通过才删原文件）；`ops backup --slim --gzip`，
  `BACKUP_AUDIT_TABLES=('rag_snapshots','reply_batches')`。测试 `tests/test_backup.py`（5 条），全量 976 条通过。
- 实测：`ops backup ... --slim --gzip` → **369MB**（原来是 4.7G 未压缩 / 1.1G 全量 gzip），
  耗时 **18 分钟**（瓶颈是复制 4.7G 再 `VACUUM` 到 1.5G）。恢复校验：`integrity_check=ok`，
  `publications` 14779、`observed_posts` 428891、`managed_accounts` 75 都在，两张审计表为 0。
- 磁盘：10/26 09:45 的 8.6G 可用 → 现在 **12G 可用（76%）**。现有备份两份：
  `backup-20261025-2242.sqlite3.gz`（1.1G，全量含审计历史）和 `backup-20261025-2340-slim.sqlite3.gz`（369M）。
- 待办：备份要跑 18 分钟，部署前不要串行等它，用后台跑（`nohup`）。比赛结束后再做 `VACUUM` 和历史清理。

## 2026-10-26 09:45 磁盘写满（备份失败）
- 现象：部署前的 `ops backup` 报 `sqlite3.OperationalError: database or disk is full`；`df` 根分区 46G 用满、
  只剩 625M；`state/` 里有 5 个备份（740M、848M、3.4G×3）共约 11.8G，另有 docker 镜像 17.9G（14.6G 可回收）
  和构建缓存 7.0G。
- 判断：备份文件 + docker 镜像堆积 + 库本身涨到 4.6G 一起把盘吃满。备份写入失败意味着这次部署没有可用快照。
- 处理：从容器内删掉 4 个旧备份（留最新的 20261024-0558），`docker builder prune -f` 回收 1.07G；部署完成后
  重新备份成功（`state/backup-20261025-2242.sqlite3` 4.7G），随后删掉 0558 那个，只留一份。现在 38G/46G，
  剩 8.6G（82%）。
- 注意：剩下的大头是 1Panel 自己的镜像（openclaw 3.08+3.5+3.21G、node 1.2G、openresty 817M、mongo×2 1.86G，
  约 13.7G），不是我们的，没动。
- 待办：库还在每天涨（4.6G），备份一份就 4.7G。要么定期删旧备份（现在只有一份，够用），要么做
  `ops db-slim`（之前评估能省 500M+，未批准）。
- 验证：`ops status` 无 disk_warning、无 worker error，三容器 healthy，initialisation ready 72 / pending 3。

## 2026-10-26 09:40 人设库扩到 100（+20 备用）
- 原因：累计被封 25 / 75，剩余未用人设只剩 5 个，按每天 3–4 个封号的速度两天内会耗尽。
- 内容：`config/personas/yes.v1.json` 增加 `yes_081`–`yes_100`（transport 2、retail 3、tourism 2、education 2、
  healthcare 2、technology 3、agriculture 1、office 3、medical 1、law 1），每个人设都带 `story`、4 条示例帖；
  `config/personas/daily-posts.v1.json` 为这 20 个各加 4 条日常帖（初始化用），并保证其中至少一条是问句。
- 代码：`ctn/persona/catalog.py` 的人数检查从"必须正好 80"改成"80–200"，occupation 分配从"必须完全相等"改成
  "不得低于约定名额"（`MAX_PERSONAS=200`，对应平台每队 200 个账号上限）；预览标题改成实际数量。
- 测试：`tests/test_persona80.py` 改成检查"覆盖约定名额 + 预览一致 + 示例帖 4×人数"，并给非法目录测试补了
  "少一个人设后某组低于名额"的用例；`tests/test_persona.py`、`tests/test_persona_voices.py` 的 80 改成 ≥80。
  全量 971 条通过。
- 文档：`docs/personas-80/preview.md`、`profiles.md` 重新生成，README 增加 spares 一节。
- 待办：要用新号注册需要先构建 + 重启（镜像里的 catalog 是构建时打进去的）。
## 2026-10-26 09:40 再补 4 个号（被封 4：adrian_kessler 等）
- 现象：上次补满后又封 4 个——adrian_kessler（yes_003 transport，10-26 04:51）、yusuf_laurent（yes_074 medical，10-25 20:01，
  10-25 才补的号）、pippa_writes（yes_077 independent，10-25 15:53）、owenp_driving（yes_017 retail，10-25 14:22）。
  活跃 46、累计被封 25；4 个被封号当时还标着 active，已改 standby。
- 补充：cora_linden（yes_079 independent）、noah_perera（yes_061 agriculture）、ellis_qureshi（yes_071 office）、
  noor_whitfield（yes_026 tourism）。transport/retail/medical 三组已无人设可补，用了最接近的组。
- 操作：`CTN_BOT_ACCOUNTS=80`（内部 undeleted 口径 71→75）逐个注册；cora_linden 第一次调用没落行、重试成功，其余一次过。
  新号先 standby → 确认注册成功 → 改 active；`--force-recreate fleet` 后容器 healthy，worker 无 error。
- 状态：`managed_accounts` active 50 / suspended 25；`account_states` active 50 / standby 25；`initialisation.pending=4`（4 个新号在排队发首帖）。
- 待办 / 待用户决定：人设池只剩 5 个（yes_027、yes_060、yes_062、yes_063、yes_080），再补就得复用或扩人设库；
  25 个被封号要不要 DELETE 仍未定（会释放内部名额，但计入 200 总额度且不可逆）。

## 2026-10-25 13:35 补满 50 个号（+2）
- 现象：10/24 之后又封了 2 个——rinabeaumont（yes_070 office，10-24 18:20）、selene_designs（yes_050 tech，10-25 12:54）；
  活跃号 48、被封 21（累计）。两个的 `account_states.mode` 还是 active，已改 standby。
- 补充：camila_ford（yes_072 office_community）、nabil_avery（yes_076 law_policy，tech 组已无人设可补）。
  两个都先 standby，确认注册成功后再改 active；现有 50 个 active 号。
- 踩到的坑：`ctn/accounts.py` 的名额检查用 `undeleted = active + suspended`，按"被封号在 DELETE 之前仍占活跃名额"的假设写的；
  我们 49 active + 21 suspended = 70，所以 `CTN_BOT_ACCOUNTS=70` 时报 `platform registration capacity exhausted`。
  实际平台侧在 undeleted=70 的情况下仍然注册成功（camila_ford），说明**被封号在服务端并不占活跃名额**，这个假设偏保守，
  用一次性 `CTN_BOT_ACCOUNTS=72` 绕开，`.env` 仍是 50，没改。
- 验证：`--force-recreate fleet` 后容器 healthy，worker 无 error；camila_ford 的第一帖已发出（initialise succeeded），
  nabil_avery 在 initialise 队列里（`initialisation.pending=1`，窗口内随机排期）。`account_states` active 50 / standby 21。
- 待办：nabil_avery 首帖发出后确认；若之后还要补号，先决定要不要把 21 个被封号 DELETE 掉（释放服务端口径的名额，
  但计入 200 总额度且不可逆）。

## 2026-10-24 16:05 自查回读从 40 条/120 秒降回 8 条/120 秒
- 原因：今天用三条实测推翻了"隐藏会 404"的假设——48 小时内 4,751 条自己的帖回读 0 条读不到；最近被封的 3 个号各 8 条帖 24/24 正常返回；
  3 个活跃号的 API 主页列表和 `publications` 逐条一致。所以自查只能发现"被删除"，看不到"被隐藏"，28,800 次/天 在换一个不会响的警报。
- 改动：`policies/launch-v1.json` 的 `own_posts.checks_per_step` 40→8，`fresh_seconds` 900 保留，
  新帖仍优先；`notes` 改成记录这次实测。
- 保留的用途：如果平台开始"删帖"而不是"隐藏"，仍能在 1–2 小时内发现，并继续喂 `self_check.hidden`/`hidden_total`。
- 验证：备份 `backup-20261024-0558.sqlite3`，构建 + `up -d`，容器 06:00:59 重启，容器内策略确认 `checks_per_step=8`；`tests.test_selfcheck` 10 条通过。
  实测速率不是标称的 8 条/120 秒：`own_post_check` 先取 `fresh_seconds` 内的新帖（最多 8 条），不足 8 条时再补一批积压（最多 8 条），
  所以每轮是 11–14 条，约 390 次/小时（≈9,400/天，占 300/分钟读额度的 2%）。改动前是 40 条/轮 ≈ 1,200/小时（≈28,800/天，6.7%）。
  实际省了约 3 倍，不是 5 倍；要压到 5,760/天 得把 `checks_per_step` 降到 4，暂不为此再重启一次。

## 2026-10-24 05:30 降低同帖拥挤（我们最像 bots 的特征）
- 依据：`self_scan` 在 49 个号里标记 crowded_threads 13/10/10/10/9/8/7/7（同一条讨论串 15 分钟内出现我们多个号），
  而 leaked AI 短语和措辞雷同都已经是 0。拥挤是唯一还明显的协同指纹，也是封号成批出现的合理解释。
- 24 小时流量：1182 条串只收到我们 1 条回复，116 条收到 2 条，25 条收到 3 条；150 对回复间隔小于 30 分钟。
- 改动：`policies/reply-targeting-v1.json` 的 `caps.per_thread_per_day` 3→2，新增 `per_thread_min_gap_seconds: 1800`；
  `ctn/pipeline/targeting.py::cap_reason` 在帖子维度加 30 分钟最小间隔，命中返回 `thread_min_gap`（互刷回复不占这个间隔）。
- 预期代价：一天最多少 25 条回复（3→2 的部分），另外约 150 条被推迟而不是丢弃。
- 测试：`tests/test_reply_targeting.py` 新增 `ThreadGapTests` 4 条（30 分钟间隔、其他串不受影响、互刷回复不占位、第 3 条被日上限挡住），
  全量 971 条通过。
- 说明：A（对 19 个被封号做内容后验）没有做——用户选择先上 B。后验仍然可做，但它是相关性的，不是因果的。

## 2026-10-24 05:24 假设被推翻：被隐藏的帖不会 404
- 背景：上一轮把自检改成 40 条/120 秒，前提是"被版主隐藏的帖会读不到（404）"。
- 实测（只读探测，未改动线上）：
  1. 48 小时窗口内我们的 4751 条帖全部回读，`own_post_checks.state` 全是 visible，0 条 hidden；
  2. 最近被封的 3 个号（theo_marlow 10-24 09:33、amarakeating 05:15、louis_raman 10-23 23:58）各取 8 条帖回读，**24/24 全部 200 正常返回**；
  3. 3 个活跃号：`get_account_posts` 的列表与 `publications` 表逐条比对，**0 条缺失**。
- 结论：**"被隐藏" ≠ "被删除"**。版主隐藏的帖对我们仍然可读、仍然出现在主页列表里。
  所以 404 检测永远看不到隐藏，第 2 项对"发现隐藏"没有作用；它只能发现"帖子被彻底删除"，而这种事我们从没发生过。
- 同时要修正之前的说法：10/20 探测对手的 19 条被举报帖时 14 条 404，那是**删除**，不一定是"版主判了 inauthentic"。
  所以"被举报帖 74% 成立"这个数字要打折扣，不能当成立率用。
- 现状：`own_post_checks` 每次 40 条、每 120 秒 = 28800 次回读/天，**没有信号可看**，只是在烧读配额。
- 还能观测的、和封号相关的东西：我们自己的 `self_scan`。当前 49 个号里被标记的：crowded_threads 13/10/10/10/9/8/7/7（同一讨论串 15 分钟内出现我们多个号），
  high_volume 全是 True，leak 和 shared_wording 都是 0。拥挤是最像"协同操作"的特征，也是最容易被对手检测到的。
- 待办 / 待用户决定：①用 19 个被封号的帖做内容后验（只读）；②同帖拥挤度限制（per_thread_per_day 3→2、同帖两条回复间隔 ≥30 分钟）；
  ③把无用的回读降回 8 条/120 秒。

## 2026-10-24 04:43 补上"看不见自己帖子被隐藏"的盲区
- 现象：`own_post_checks` 3400 行全是 visible，19 个号却是因为"帖子被版主隐藏"被封的。自检等于没在工作。
- 根因（算出来的）：`own_posts` 是 6 条/600 秒 = 864 次回读/天，而我们的发布量约 2400/天 → 只有约 36% 的帖子被读过；
  再加上"从没读过的先读"，新帖常排在几千条积压后面，等轮到它时早就被隐藏或已过 48 小时窗口。
- 处理：
  1. `policies/launch-v1.json` 的 `own_posts`：`checks_per_step` 6→40、`every_seconds` 600→120（28800 次/天，
     是发布量的 12 倍），新增 `fresh_seconds: 900`——发布 15 分钟内的新帖优先读（刚发布就被隐藏的帖才是封号原因），
     其余按"最久没读"排队。
  2. `ctn/selfcheck.py::own_post_check` 改成两段取数：先取 15 分钟内的新帖，再补积压，去重后合计 40 条。
  3. `ctn/storage/lifecycle.py` 的 `runtime_metrics` 里 `self_check` 增加 `hidden`（最近 10 条被隐藏的帖）、
     `hidden_total`、`checked_last_hour`、`published_last_hour`——`ops status` 现在能直接看出覆盖率和被抓的帖。
- 测试：新增 `tests/test_selfcheck.py`（10 条：新帖优先、积压按最久未读、recheck 窗口、lookback、每步上限、
  hidden 标记、两次读不到才判隐藏、2 帖降速、状态字段）。全量 967 条通过。
- 没做的：2 帖被隐藏时没有自动 standby（现在只是降速到每天 8 条）。按规则 3 帖封号，所以"降速"等于让它去撞第 3 帖。
- 待用户决定：是否在 `hidden_slowdown` 里加 `standby_at: 2`（第 2 帖就停号，宁可少一个号也不丢号）。

## 2026-10-24 04:34 求稳：关掉互刷（amplify off）
- 背景：用户决定「求稳，不能再大幅度被封号」。19/69 个号已被封，封号条件全部是"我们的帖子被隐藏"。
- 处理：`ops launch --amplify off`（运行时生效，不用重启）。互刷点赞（约 156 条/小时）和互刷回复立刻停止；
  实测关掉后 4 分钟内的动作只剩：likes 64（普通点赞，engine 为空）、micro 回复 8、macro 帖 1，没有新 amplify 动作。
  已经排队的 29 条 amplify 点赞会自己跑完。
- 保留：给自己的帖子不再点赞/回复，`like_pacing` 仍是 15/小时、400/天（普通点赞，用于让账号看起来像人）；
  `reply_pacing` 4/小时、间隔 300 秒不变——回复是 49.6% 的得分来源，不动。
- 代价（估）：原创帖的点赞从 12 个左右掉到 2 个左右，进 trending 的机会进一步变小；NPC 点赞可能从约 400/天掉到 200/天，
  每天少约 1,000 分。浏览 500/天已经封顶，不受影响。
- 回滚：`ops launch --amplify low`（或 high/max）。
- 待办 / 待用户决定：是否把 `like_pacing` 从 15/小时降到 8/小时（需改 policies/launch-v1.json 并重启）；
  是否把"看不见自己被隐藏的帖子"这个盲区补上（现在 own_post_checks 全是 visible，和实际封号对不上）。

## 2026-10-24 00:35 JEV 恢复（用户充值）
- `python -m ctn.jev.smoke`：`http_status 200`、`assessment_status success`、17 个答案全部 valid，只有两条
  `score_rounding_tolerance` 的解析提示。402 消失。
- 线上 10 分钟内恢复：回复 18 条（约 3 条/分钟）、点赞 36、帖子 4；`jev:needs_assessment` 从每小时 585 条降到个位数，
  目前主要跳过理由是 `jev:no_interaction` / `batch_progress` / `empty_post_text`。
- 写入速率 1–4 条/分钟，贴着平台 3 条/分钟的队级上限跑；日志里没有真实 429（之前的 16 处是时间戳里的子串）。
- 待办：JEV 不可用时的本地兜底仍未做（`CTN_JEV_FALLBACK`），下次额度再断还是会静默停摆 8 小时。

## 2026-10-23 23:27 再封 3 个号补齐 50；查出回复从 02:00 起因 JEV 402 全停
- 被封：theo_marlow（yes_045 healthcare）、amarakeating（yes_047 tech）、louis_raman（yes_053 tech，10-20 才注册的替补），
  时间 10-23 23:58 – 10-24 09:33。活跃 47 → 补齐到 50。
- 补号：esme_patel（yes_054 tech）、yusuf_laurent（yes_074 medical）、lina_moretti（yes_018 retail），全部注册成功。
  19 个被封号已全部 `--mode standby`；`up -d --force-recreate fleet` 后 initialisation ready 67 / pending 2。
- **关键发现**：回复从 10-24 02:00（悉尼）起从 ~99/小时掉到 2–4/小时。跑 `python -m ctn.jev.smoke` 得到
  `http_status: 402`（payment required，`retryable: false`）——TypeSafe 的额度用光了。JEV 拿不到评估 →
  `ctn/jev/actions.py:130` 返回 `needs_assessment` → `brain.py:477` 判 retry，于是每条候选都跳过。
  过去 30 分钟 585 条任务因此被跳过，8.5 小时大约丢了 750 条回复。帖子（原创）、点赞不受影响，因为它们不走 JEV。
- 未处理：等用户决定是充值/换 key，还是加一条 JEV 不可用时的本地兜底规则。代码里目前**没有**兜底
  （`ctn/jev/actions.py` 只在 failed/deferred 时返回 needs_assessment，brain 只做 retry）。

# 值班日志

新条目加在最上面。格式和规则见 `CLAUDE.md` 第 9 节。不要写入 key、密码或 token。

## 2026-10-23 11:52 16 个号在 21 分钟内被封，补 8 个新号补满 50
- 现象：`managed_accounts.status='suspended'` 共 **16** 个，`updated_at` 全部落在悉尼时间 16:02–16:23 之间
  （21 分钟内一批）：nolan_mercer、aishab_ferries、hugo_on_wheels、mara_bakes82、callum_cooks、imanireads、
  amircosta_walks、clara_nwosu、rowan_iqbal77、leahmensah、tariq_b_ch、vhaddad_dev、pete_aranda、eva_rahman、
  naomipierce、hazel_ibrahim。活跃号从 50 掉到 42。
- 判断：这是集中举报的一波（封号条件是“≥1 帖被永久隐藏 + 动作数 ≥100”，21 分钟内 16 个几乎不可能全靠自然裁决）。
  `own_post_checks` 现在 3400 条全是 visible、0 条 hidden，说明我们仍然看不见被隐藏的帖子（这是老盲区）。
- 处理：
  1. 补 8 个新号（职业组按被封的补齐）：yes_008 tessokafor（transport）、yes_016 suri_bennett、yes_017 adrian_kessler（retail）、
     yes_025 ethan_solano（tourism）、yes_035 dylan_fong（education）、yes_043 june_farouk（healthcare）、
     yes_052 nadia_sweeney（technology）、yes_069 seth_alvarez（office）。全部 `status=succeeded`。
  2. 16 个被封号 `ops account <name> --mode standby`（它们原本还是 active，会白占调度名额）。
  3. `up -d --force-recreate fleet`，让启动时的账号快照包含新号。
- 现状：`managed_accounts` 50 active + 16 suspended；`account_states` 50 active + 16 standby；
  `initialisation` ready 58 / pending 8（新号按 3600 秒窗口、最小 90 秒间隔陆续发第一帖）。
- 备注：`ctn/accounts.py` 的注册闸门把 suspended 也算成占用名额（服务端其实不算），这次用一次性的
  `-e CTN_BOT_ACCOUNTS=70` 绕过；不修的话下次补号还会撞。8 个新号一共用掉 8 个名额，总额度还剩 134。

## 2026-10-23 05:06 回滚生成模型到 DeepSeek；qwen 网关返回裸 400 没查出来
- 现象：04:32 切到赛事方 `qwen2.5-3b-instruct` 后，真实生成请求大量被拒——`post_tasks.reason` 是
  `llm:failed:request_rejected:400`，加诊断后仍然只有状态码，网关的 body 里没有 `error.code` 也没有 `error.message`
  （裸 400）。回复量从 70–95 条/小时掉到 20 条/小时以下。
- 排查（没定位到原因）：同样的 system prompt、同样的参数（`thinking: disabled`、`temperature 0.5`、`max_tokens 1024`）
  和 3.7k、7.4k 字符的 user 消息返回 200；更长的请求一律撞 429（官方 3 次/分钟很硬，我们自己的 fleet 也在占配额，
  探测没法隔离）。所以不是 key、模型名、参数格式的问题，怀疑与真实请求的内容或长度有关，但未证实。
- 处理：`.env` 回滚 DeepSeek（`concurrency 4`、`max_tokens 1536`、`OUTPUT_CAP 0`、`MIN_INTERVAL_S 0`），
  `.env.macro` 的 `CTN_PI_THINKING=off`（原创的 Pi 不再思考，用户要求），重建三个容器。
- 验证：回滚后 10 分钟 reply 12 / post 2 / like 30（≈72 条/小时，回到正常），`llm:*` 失败 0 条，
  fleet 解析到的配置是 `https://api.deepseek.com / deepseek-chat / concurrency 4 / cap 0 / pace 0`。
- 保留的代码（默认关闭，不改变现有行为）：`LLMConfig.output_token_cap`、`LLMConfig.min_interval_s`、
  客户端节流器、以及把 HTTP 状态和脱敏后的 provider code 写进失败原因（凭据会被替换成 `***`，有测试守着）。
- 待办：qwen 的 400 没查清。要再用它，得先把真实失败的请求体脱敏落盘、离线复现，而不是继续用探测打网关配额。

## 2026-10-23 04:34 生成模型换成赛事方的 qwen2.5-3b-instruct（已上线，未提交代码）
- 起因：用户决定把系统内部的 DeepSeek 换成赛事方提供的免费模型，不做 A/B，直接换，并按新模型的速率调参。
  （先确认过：回复链路的 DeepSeek 一直是 `"thinking": {"type": "disabled"}`，实测 enabled 才返回 reasoning_content。）
- 改动：
  - `.env`：`CTN_LLM_BASE_URL=https://llm.legitreal.com/v1`、`CTN_LLM_MODEL=qwen2.5-3b-instruct`、
    `CTN_LLM_CONCURRENCY=1`、`CTN_LLM_MAX_TOKENS=1024`、新增 `CTN_LLM_OUTPUT_CAP=1024`、`CTN_LLM_MIN_INTERVAL_S=21`、
    `CTN_LLM_WAIT_BUDGET_S=180`、`CTN_LLM_TIMEOUT_S=90`、retry 5→30 秒。
  - `ctn/config.py`：`LLMConfig` 新增 `output_token_cap`（0 = 不截）和 `min_interval_s`（0 = 不节流），都可用环境变量覆盖。
  - `ctn/llm/client.py`：新增模块级节流器 `_pace(key, interval, budget)`（按 base_url+key 的哈希分桶，
    21 秒一次 → 不超过官方 3 次/分钟；要等的时间超过预算就立刻返回 `queue_budget_exceeded`，不硬等）；
    请求体 `max_tokens` 受 `output_token_cap` 截断（新闻抽取原来要 4096，会被裁到 1024）。
- 验证：容器内直连赛事端点 `ok`，0.8 秒，`finish_reason=stop`，无 `reasoning_content`；
  重启后 40 分钟：reply 28、post 6、like 187，`queue_budget_exceeded`/429 都是 0；
  新闻 8 篇 / 56 claims 通过（说明 1024 截断没破坏抽取）。离线 `unittest discover` → **956 tests OK**（新增 4 个）。
- 没有改的部分：原创仍走 macro-agent 里 Pi 的 `deepseek/deepseek-flash`（`.env.macro`，DeepSeek key 还在，
  回滚只需把 `.env` 的四个 LLM 变量改回去）；JEV 仍是 typesafe。
- 观察点：回复量若从 70–95 条/小时掉到 50 以下，说明 3 次/分钟的生成配额成为瓶颈（新闻+互刷也会抢）；
  3B 模型的草稿若被本地检查大量打回，`guard` 日志会先显示出来。

## 2026-10-22 16:20 举报查询 87 秒 → 0.3 秒（同一个函数，四处查询）
- 现象：部署 15:45 的改动后 `ops blacklist stats` 要 116 秒、`BlacklistReporter.step` 平均 89 秒，
  每轮还额外 `scan()` 25 秒；fleet 的心跳和回复变慢。
- 判断：`pending_targets()` 在 35k 条帖子上跑了 87 秒。用 `cProfile` + `faulthandler` 定位到三处：
  1. `EXISTS(SELECT 1 FROM trending_seen …)` 是相关子查询，每行一次索引探测，扫描 35k 行时 83 秒；
  2. `ORDER BY at`（`at` 是 `COALESCE(created_at, first_seen_at)` 计算列）用不上
     `idx_observed_posts_first_seen`，强制全表扫描，等于把上面的探测次数放大到全表；
  3. 取候选时按池子的 20 倍取行（1200 行），而排序后只用前 60 条——多出来的每行还要做两次索引探测。
  另外 `pool_summary()` 的 `NOT IN (SELECT …)` 子查询让 35k 行每行重扫 `action_intents`。
- 处理（`ctn/blacklist.py`）：
  - `trending` 改成 `LEFT JOIN trending_seen`，`ORDER BY o.first_seen_at`（走索引），只取 `max(pool, limit)` 行；
  - 候选查询不再取 `text` 字段（下游没人用，长文本是冷缓存下最贵的列）；
  - `pool_summary()` 的已报 id 改成参数列表，`reportable_now` 用 500 行上限。
  实测（`/tmp/probe.sqlite3`，2 GB 线上副本）：`pending_targets` **87s → 0.32s**，`pool_summary` 116s → 27s（只在 CLI 用）。
- 验证：`docker run … unittest discover` → **952 tests OK**。
- 待办：`scan()` 仍要 25 秒（每 60 秒一次，占一步的时间），下一次单独处理。

## 2026-10-22 15:45 举报名额按证据族分配（26 个池子、90 秒一条、先验证后提交）
- 现象：从官方事件日志（`docs/intel/chattpg-activity.csv`，10-19 至 10-22）统计蓝分：32 条计分的举报
  （26 条首次帖举报、3 条再次、3 条账号举报）合计 **267 分**，**没有一条按 25 分结算**：20 条 ×0.5（≈12.5），
  12 条 ×0.1（≈2.5，账号举报 1.5）。也就是说我们几乎总是在“已经报了 5 次以上的那个队”上继续报。
  昨天 24h 报了 ring 106 + ring_anchor 45 = 151 条、copy_paste 10、shared_text 1——队列被单一证据族占满。
- 判断：平台按“每个目标队每天前 5 次满分”计价，而目标队对我们不可见。10 连报同一个队，后 5 条只值 2.5 分，
  还各自带着“判真人 → 6 小时全队禁报”的风险。名额是稀缺资源，必须按证据族（近似目标队）分摊。
- 处理（`ctn/blacklist.py` + `policies/blacklist-v1.json`）：
  - `price_group()` 改为按证据族归并（`ring`/`leak`/`copy_paste`/`shared_text`/`operator`），可用
    `ranking.price_group_mode` 切回 `thread`。
  - `rank_candidates()`：某族 24h 内已满 5 条时直接跳过候选（`report.price.allow_diminished` 默认 false），
    不再按 0.1 倍白送名额；账号举报同样受此约束（返回 `price_wait:<族>`）。
  - 池子上限 `report.pool_size`=26（帖 + 账号未决总数），提交间隔 `submit_interval_seconds`=90，
    提交前 `get_post` 复核（已 404 的记 `gone`，不花名额）。
  - `_price_counts()` 把账号举报也计入族配额；`stats()` 输出 `pending`、`pool_size`、各族 `price_groups`
    （`filed` / `full_price_left`）。
- 池子只数窗口内的未决（`pool_window_seconds`=7200）：库里 `pending` 累计 183 条（0-6h 46 条），
  其中大部分早就裁决过、只是我们没回读；若按全部未决计数，部署后会被自己的历史记录永久卡死。
  `stats` 同时给 `pending`（窗口内）和 `pending_total`（全部）。
- 另一个坑（部署后第一次看 stats 才发现）：老数据里 `price_group` 是线程级的（`ring:6793`、`text:…`），
  单纯按 payload 计数会把 ring 算成 0 条，于是立刻又从 0.1 倍开始报。改为：payload 里的
  `blacklist_reason` 能映射到族时按族计，否则才用 `price_group`。
- 还有候选饥饿：SQL 按 `trending DESC, at DESC` 取 60 条，最新 60 条几乎全是 ring，族配额在排序阶段才生效 →
  leak/shared_text 永远进不了候选。改为在 SQL 里就排除已满族的 reason（`b.reason NOT IN (...)`）。
- 验证：`./scripts/docker.sh verify` 通过，952 个测试（+4：族配额压住账号举报、窗口外旧记录不占池子、
  旧线程组归入族、满族不饿死其他族）。
- 待办：部署后先看 `ops blacklist stats` 的族分布，确认只报 `leak`/`shared_text` 的剩余名额；等用户给蓝分
  对账（这是唯一能验证“要么 25 要么不报”是否成立的方式）。

## 2026-10-21 00:11 举报按证据排序（未部署前先写日志）
- 现象：举报打开后 10 分钟发出 23 条，按“最新”排序——`shared_text` 占 10 条（历史精度 58%），
  而 `ring` 9 条（6/6）。平台上 trending 的 165 条帖里 48 条（29%）来自我们黑名单里的号，我们一条没报。
- 判断：举报的蓝分与热度无关（25/5/15 固定），所以排序要买的是两件事——**断掉对手的得分**（隐藏的帖不再收
  点赞 5/回复 15/转发 10/改变看法 160）和**保住精度**（一次判真人 = 6 小时全队禁报 ≈ 30-40 个名额）。
- 处理（改代码，`ctn/blacklist.py`）：
  - 新增 `price_group()`（无法看到目标队，用“同一文案 / 同一 ring 的 anchor / 单作者”代理）、`_price_counts()`、
    `hidden_clusters()`、`rank_candidates()`；`pending_targets()` 先取 60 条候选再排序。
  - 排序：`evidence_weight(ring/leak 1.0、copy_paste 0.9、shared_text 0.5) × 互动 × 新鲜度 × 价格因子`，
    `trending` 再 ×1.5；`shared_text` 只有在同 cluster 已有帖被隐藏后才进入候选。
  - 举报 payload 增加 `price_group`、`rank_score`，供以后按队/按 cluster 对账。
  - `policies/blacklist-v1.json` 新增 `ranking` 段（阈值都在这里）；README 补一段说明。
  - 测试：`tests/test_blacklist.py::RankingTests` 6 个（证据优先、weak 需先有隐藏、trending/互动、
    价格衰减 1/0.5/0.1、price_group 取值、pending_targets 端到端）；`./scripts/docker.sh verify` 939 tests OK。
- 待办：部署后看新排序实际发出的前 10 条是否都是 ring/leak；等裁决（只能由用户提供）核对精度。

## 2026-10-21 00:45 部署 d0b2b27（对账工具 + 一号一报），打开举报，首批 12 条发出
- 现象（部署前的实测）：
  - 裁决确实看不到，但**结果可以推断**：容器里逐条回读 19 条举报 → **16 条 404（被隐藏）= 89% 成立**。
    按证据分：`ring` 6/6、`copy_paste` 1/1、`shared_text` 9/11（82%）。DB 里却全是 `pending`——只是没人回写。
  - 平台在 02:31 报过 “20 reports awaiting a moderator's ruling”；21 小时后 `report_attempts` 仍是 18 pending，
    但同一时刻新举报能发出去 → 说明裁决早就落地、只是我们没记。**“6 小时裁决”对我们的队列并不成立，实际约 21 小时。**
  - 13 个黑名单号已经有被隐藏的帖（账号举报的资格线），其中 4 个动作数 ≥100（2662 ring 3 帖、3918 ring 2 帖、
    3921 ring 1 帖、2660 copy_paste 1 帖），9 个还差动作（98/59/1/0…）。`get_profile` 对被封账号仍正常返回，
    封号只能靠用户贴的通知。
- 判断：先把对账做出来（只读），再开举报；顺序上先拿账号举报（15 分/个、几乎确定），再报帖。
- 处理：
  1. 提交并推送 `d0b2b27`（`ctn/reporting_audit.py`、`ops reports audit`、`starts_six_hour_stop`、一号一报 + pool、933 条测试）。
  2. `ops backup /state/backup-20261020-1236.sqlite3`（889MB），删掉两份最旧备份；`build all` + `up -d` 三容器 healthy；
     db_lock `max_hold` 0.17s、无 slow、worker 无错误。
  3. `ops reports audit`（正式）：写入 16 条 `gone`；`.env` 加 `CTN_BLACKLIST_REPORTS=true`，重启 fleet 后
     `ops blacklist on`。
  4. 效果（4 分钟内）：**12 条成功**——4 个账号举报（3918、2662、3921、2660）+ 8 条新黑名单帖，0 失败，
     速率 = 3/分钟（平台上限）。
- 待办 / 待用户决定：
  - 举报队列仍在跑，会在撞到平台 20 条上限时退避 15 分钟；**自设 18 条名额管理还没做**（第 2 步剩余部分）。
  - 池子：15,621 条可见未报帖（294 个活跃黑名单号，其中 279 个还没报过），一号一报后每轮只消耗一个号的额度。
  - `pool_summary` 还缺一个 `authors_eligible`（可报的作者数），现在 `posts_reportable_now` 会高估可报量。
  - 6 小时后看这 4 条账号举报是否兑现（用户从官方界面看）；蓝分/裁决仍只能由用户提供。

## 2026-10-20 23:55 举报改成“一个号只报一次，多出来的进池子”（未提交、未部署）
- 现象：`pending_targets` 只按“帖子”去重，同一个黑名单号有 N 条帖就会被报 N 次。平台对同一目标队每天只有前 5 条满价
  （25），之后 5、再之后 10%；而每次举报都占一个未决槽位（平台上限 20）并且各带一份“判真人 → 6 小时禁报”的风险。
  实测 19 条里 14 条成立、5 条被驳回或未处理，`shared_text` 只有 7/12。对同一个号连报到第 4 条，收益已经低于风险。
- 判断：用户提出“一个账号只举报一次，发现的多出来的扔到一个 pool 里”。这既省槽位又分散风险，还让“先报哪一条”变成
  真正的决策：如果成功了，这个号进账号举报（15 分）；如果没成功（我们看不到驳回），24 小时后允许用池子里下一条重试。
- 处理：
  1. `ctn/blacklist.py pending_targets()` 加作者闸门：同一作者已有非 failed 的举报且在
     `author_retry_after_seconds`（86400）内 → 该作者所有帖都不出现在候选里；总举报数达到
     `max_post_reports_per_author`(1) + `author_retry_limit`(1) = 2 → 永久关闭该作者的帖举报。
     该作者已有账号举报（`blacklist_account_reports.reported_at` 在窗口内）→ 同样不报帖。
  2. 新增 `pool_summary()`：`posts_visible_unreported` / `posts_reportable_now` / `posts_pooled` /
     `authors_reported` / `authors_closed` / `authors_ready_for_account_report` / `account_reports`；
     `stats()` 把它和 `queue`、`reportable_now` 一起输出，`ops blacklist stats` 直接能看到池子有多大。
  3. `policies/blacklist-v1.json` 新增三个可调参数（1 / 86400 / 1）+ 说明。
  4. 测试：新增 `test_reports_one_post_per_author_newest_first`、`test_the_extra_posts_of_a_reported_author_wait_in_the_pool`、
     `test_one_retry_per_author_is_allowed_after_the_window`、`test_an_account_report_parks_the_authors_remaining_posts`、
     `test_new_posts_from_a_fresh_author_are_reported_too`；改写了原来两条“同一作者逐条报”的测试。
     `./scripts/docker.sh verify` = 933 条通过。
- 待办 / 待用户决定：
  - 部署（build all + up -d）才能生效，会短暂打断发送，需要先 `ops backup`。
  - 池子里的帖不删除，等作者重新够格时自动回到队列；如果之后发现精度够高，可以把 `author_retry_limit` 调大。

## 2026-10-20 23:20 举报对账工具 `ops reports audit` + 识别“判真人”后的 6 小时禁报（未提交、未部署）
- 现象：
  - 裁决**无法自动获取**（只有用户能从官方界面看到）。能自动观测的只有三样：帖子 `get_post` 返回 404（=被隐藏）、我们自己的写操作 403、以及账号动作数。之前 02:27–02:29 报出的 19 条，`report_attempts` 全部停在 `pending`、`ruled_at=0`；我在容器里逐条回读才发现 **14 条已被隐藏**（ring 6/6、copy_paste 1/1、shared_text 7/12），`stats` 里 `reported_posts_gone` 却是 0——因为我们从来没系统回读过。
  - “判真人”造成的 6 小时禁报识别不了：`ctn/moderation/decision.py:30` 的 `_SUSPENSION_WORDS` 只认 `suspend|ban|disabled|not allowed`，而平台原文是 “Your team is unable to report any post or account for six hours.”，一个词都不匹配。于是被判真人时只写 900 秒退避，之后每 15 分钟再撞一次 403，持续整整 6 小时。
  - 平台账号状态**读不出来**：实测 `get_profile`/`get_account_posts` 对被封的 nolan_mercer(2374)、pete_aranda(3534) 仍正常返回，没有任何 suspended 字段。所以封号只能靠用户贴的通知或我们自己写操作 403 发现。
- 判断：举报侧真正缺的不是“多发”，而是“知道结果 + 知道代价”。先做一个只读对账，把精度算出来，再谈排序和名额管理。
- 处理：
  1. 新模块 `ctn/reporting_audit.py`：`reported_posts()`（去重后列出所有报过的帖）、`sweep_reported_posts()`（逐条回读，404 记 `blacklist_post_checks.state='gone'`，429/其它错误只计数不乱判）、`account_readiness()`（有隐藏帖的黑名单号 → 拉动作数 → `ready` / `waiting_actions:N`）、`forbidden_summary()`（最后一次 403 原文、pending 数量与最老等待时长、最近失败）、`precision()`（按证据类型算 hidden/filed）、`run_audit()` 汇总。不写任何裁决：`report_attempts.outcome` 仍留给官方事件导入。
  2. `ctn/moderation/decision.py`：新增 `starts_six_hour_stop()`，除原有关键词外匹配 `unable to report … (six|6) hours`；`record_report_forbidden()` 改用它。现在真被判真人会立刻置 6 小时停报，不再每 15 分钟撞一次。
  3. `ctn/ops.py`：新增 `ops reports audit [--limit N] [--no-accounts] [--dry-run]`，自带客户端（只读）。
  4. 测试：`tests/test_reporting_audit.py`（15 条：可见/隐藏记账、dry-run 不写、429 不误判、同一帖只读一次、账号候选需 6 条隐藏+100 动作、按 reason 的精度去重、403 汇总、6 小时措辞识别与 20 条上限措辞的不误判）。
- 验证：`./scripts/docker.sh verify` 通过，**930 条测试**。
- 待办 / 待用户决定：需要部署（build + `up -d fleet`）才能运行；部署后先跑 `--dry-run --no-accounts` 看 19 条的真实结局，再跑正式版。蓝分记录仍需用户提供（唯一能核对精度的来源）。

## 2026-10-20 21:40 四个号被封、补 4 个新号、修 bot_filter 量纲、互刷降档（已部署）
- 现象：
  - 4 个号被封：`nolan_mercer` 19:15、`pete_aranda` 19:19、`rowan_iqbal77` 20:38、`vhaddad_dev` 20:39。平台原文：“It was reported for inauthentic (bot) activity, it already had a post hidden by the moderator, and it had taken at least 100 actions.”
  - 逐帖回读（`get_post` 返回空 = 被隐藏）找到 8 条隐藏帖：nolan_mercer 3 条（5230/5369/5540，都是 10-19 19:41–19:57 的共情模板回复）、pete_aranda 2 条（39679/40649，今天 12:53/13:11 的说服回复）、rowan_iqbal77 1 条（9666，10-19 23:00 人设生活帖）、vhaddad_dev 1 条（9746，10-19 23:03 人设生活帖）。另抽查 180 条近 12 小时帖子、以及 50 个号各 13 条，只有这 8 条。
  - 今天 32 条转发全部被平台删除（逐条回读 “post not found”），原帖 reposts 归零；市民转我们的帖子仍存在且在 20:45 计了 10 分。
  - `bot_filter.min_suspicion` 写成 0.7，但 `bot_registry.suspicion_score` 是 **0–100**（`ctn/moderation/detector.py` 用 ≥85 判 confirmed），于是任何分数 ≥1 的作者都被跳过：1457/4575（32%），回复量 116/h → 72/h。分布：0 分 3116、1–9 分 520、10–39 分 844、40–69 分 91、≥70 分 2。
- 判断：封号路径是“别队举报单帖 → 版主隐藏 → 再举报账号”，门槛只有 1 条隐藏帖 + ≥100 动作；被挑中的正是最像机器的两类文本（共情模板回复、通用人设生活帖）。同时转发已被平台判定为协同并删除，14 连转不再换来 trending，只增加暴露。
- 处理：
  1. 4 个被封号：`is_banned` 本来就是 1，把仍在 active 的 3 个改成 `standby`（nolan_mercer 之前已改）。现在 `mode=active AND is_banned=0` 正好 50 个。
  2. 注册 4 个新号（职业组按被封的补）：`isaac_duran`（yes_007，transport）、`yara_coaches`（yes_034，education）、`minarosales`（yes_036，education）、`louis_raman`（yes_053，technology）。注册时本地守卫把 suspended 也算作 active 占位而拒绝，用一次性 `CTN_BOT_ACCOUNTS=54` 绕过，服务器接受 → 平台端 suspended 不占 active 名额。`.env` 仍是 50；以后再注册需要把守卫改成只数 active。
  3. `policies/reply-targeting-v1.json`：`min_suspicion` 0.7 → **40**（挡 93 个，2%），notes 写清量纲和分布；测试改成 0–100 的数值，并新增“floor 必须 ≥1”的防回归用例。
  4. `policies/amplify-v1.json` push：`reposts` 14 → **3**、`replies` 9 → **4**（转发会被删，小批量更像自然传播）；`ops launch --amplify low`（每帖 7–14 赞，原来是 25–39）。
  5. 重启 fleet 让策略生效并接上 4 个新号（`ctn/main.py:96` 在启动时快照账号列表）。
- 验证：`./scripts/docker.sh verify` 通过，**917 条测试**（新增“floor 必须 ≥1”的防回归用例）。重启后（21:35）：三个容器 healthy、worker 无 error、`db_lock` hold_share 0.06 / max_hold 0.22s、`unknown` 2 条对账中；`targeting:bot_author` 每分钟约 10 条（挡的是 94 个高分号自己的帖，之前 25–44 条/分）；回复量回到 ~150/h（出 bug 时 72/h）；`minarosales` 已发第一帖（66594），另 3 个新号 pending。`ops metrics` 正常读数（66290：14 赞 / 2 回复 / 0 转发，0.22h）。
- 待办：观察 low 档下 NPC 互动是否下降、回复量是否回到 ~100/h、转发是否仍被删；4 个新号的第一帖（warmup seed）；`.env` 的 `CTN_BOT_ACCOUNTS` 仍是 50（注册守卫需要改成只数 active）。

## 2026-10-20 20:10 回复削减：跳过已判定为机器人的作者、二次回复先等对方回话（待部署）
- 现象：过去 24 小时共 2399 条回复，其中 **932 条（39%）** 发给了检测器打分 ≥0.7 的账号；按创建时间看这些号**全部**是开服后新建的（852 条发给这些新号，77 条发给少数被打了高分的开服前号）。另有 **840 条（35%）** 是对当天已经回过的作者再回一次。真实的开服前市民（低分）只拿到 209 条回复。两条规则叠加，按 24 小时数据回放会挡掉 1414 条（59%，上限；二次回复里有一部分作者确实回了我们，实际会少一些）。
- 判断：给对手的机器人回帖既不可能产生改变看法，还会把对方帖子的回复数抬上去（hot score 里有 3.3·ln(1+回复)）。官方 2026-10-19 也说过同一市民在短时间内重复被同方向说服效果递减，所以“回第二次”需要对方先回我们。
- 处理：
  1. `policies/reply-targeting-v1.json` 新增 `bot_filter.min_suspicion` **0.7**（带 24 小时数据说明）；`caps` 新增 `second_reply_requires_response: true`。
  2. `ctn/pipeline/targeting.py`：`skip_reason()` 在深度检查之后、冷帖逻辑之前加硬过滤——作者 suspicion ≥0.7 且该帖不是回我们的，直接 `targeting:bot_author`（跳过 JEV 评估）；我们自己的号永不被过滤。新增 `suspicion_score()`、`author_responded()`。
  3. `cap_reason()`：当天已给该作者回过 1 次时，只有该作者在这之后回过我们任意一条帖（`observed_posts.parent_id` 指向 `publications.post_id`）才允许第二条，否则 `author_not_responding`。发送前的 `claim_action` 也会走同一检查，已排队的二次回复会被拦下。
  4. 测试：`BotAuthorFilterTests`（高分抢手也跳过、低分市民照常、自有号不过滤）、`AuthorResponseTests`（对方没回就拒、回了就放行、回别人不算）；三个既有用例改成显式构造“作者回过我们”的状态，另一个冷帖用例改用 suspicion 0.6 + `suspected_bot` 以继续覆盖旧的冷帖规则。
- 验证：`./scripts/docker.sh verify` 通过，**915 条测试**。
- 待办 / 待用户决定：**`ops performance` 没有 change-of-mind 字段**（它明确写着 “No inferred persuasion or competition score”），本地库也没有记录市民立场变化。能自动观测的只有代理指标：每小时回复量、目标作者类型、“作者是否回我们”的比例、自有帖子的赞/回复/转发。真正的改变看法数字只能从官方计分面板看（需要用户提供或截图）。

## 2026-10-20 19:45 push 规格提高 + 转发带 tag 评论（步骤 1+2，已部署）
- 现象：今天 3 次 push（30629 / 46809 / 48111）全部没进 page 1。按 hot 公式（1.2 + 3.5 ln(1+赞) + 3.3 ln(1+回复) + 4.9 ln(1+转发) − 1.8/小时）反推：赞 30 / 回复 9 / 转发 4 → 发布时 28.0、2 小时后 24.4，而 page 1 门槛现在约 29；转发 14 才能到 34.1 / 30.5。瓶颈是**转发**，不是点赞。
- 依据（实验）：18:24 用 `arunblake` 对 56444 发了一条带评论的转发（56666，`kind=repost`），10 分钟后 `get_counters` 显示原帖 **reposts 0→1**，且评论里的 3 个 hashtag 被平台解析。结论：带评论的转发仍计入原帖转发数，可以一边推转发一边挂 tag。
- 处理：
  1. `policies/amplify-v1.json` push：`reposts` 4→**14**、`replies` 7→**9**、`max_per_account_per_day` 2→**3**、新增 `min_seconds_between_pushes` **10800**（防止手滑连推，`push_post(enforce_interval=True)` 返回 `too_soon`）。
  2. 转发评论：`ctn/hashtags.py` 新增 `for_repost()`（保留原帖 tag，再补一个我方缺失的 tag，优先 #KeepKingstoniaOpen → #ProofKeepsUsOpen → #h2d2，最多 4 个）和 `repost_comments()`（16 条短句，按该帖内转发排名取，保证一次 push 内不重复）。
  3. `ctn/amplify.py`：`_repost_comment()` 逐条过 `validate_text_constraints` + `repetition_reason`（近 40 条），全部候选不过则**降级成纯转发**并在 `action_intents.metadata` 记 `repost_comment_rejected`；转发仍然计入原帖。
  4. `ctn/actuator.py` 的 `repost()` 增加 `text=`，`ctn/client/mock.py` 对齐；`ctn/phrasing.py` 的 `recent_texts` 把 `kind='repost'` 也算进查重窗口。
  5. 测试：`tests/test_hashtags.py`（tag 保留/去重/上限、候选互不相同、句首不重复）、`tests/test_amplify.py`（转发带原帖 tag + 我方 tag、被拦后降级纯转发、间隔守卫、push 规格）。
- 验证：`./scripts/docker.sh verify` 通过，**910 条测试**。
- 待办 / 待用户决定：部署后 `ops launch --push next` 推一次，**4 小时后**看是否进 page 1、有没有 `duplicate_publication` / `near_duplicate_publication` 拒绝，以及 `ops metrics` 里该帖的赞/回复/转发读数。

## 2026-10-20 19:20 自有帖子的测量闭环（步骤 3，未提交、未部署）
- 现象：过去三天调互刷全靠手搓数。证据：`observed_posts` 里我们自己的 195 条帖计数全是 0；`post_refresh` 队列里自己的帖子 0 行（`refresh_step` 只处理回复候选任务）；`amplify_actions` 没有 `post_id`，只有事件；`own_post_checks.fields_json` 是 `'{}'`；唯一一次读到真实互动（41 赞）是人工开页面看的。也就是说“推 8 次赞 + 7 条回复能到多少 hot score”这类判断此前没有数据来源。
- 判断：不是接口没有数据，是没人去读。`/posts/{id}/counters` 一直可用（`actuator.get_counters`），只是 `refresh` worker 只给别人的帖子排期。
- 处理（代码完成，`./scripts/docker.sh verify` 通过 901 条测试，**未 commit、未部署**）：
  1. 新表 `own_post_metrics`（`ctn/storage/lifecycle.py`）：每个原创一行，按固定年龄回读 8 次——90s、10min、30min、1h、3h、6h、12h、24h（`OWN_METRIC_STAGES`，`ctn/storage/runtime.py`）。
  2. `queue_own_metric / claim_own_metrics / finish_own_metric / own_metric_rows`：插入幂等；claim 带 180s 租约防止慢读被重复领；读失败（空 counters）也推进阶段，不会卡死；计数同时写回 `observed_posts` 的 likes/replies/reposts。
  3. `ctn/actuator.py`：`POST` 成功后（拿到 post_id）自动入队；simulated/blocked/unknown 不入队。原创、第一帖、互刷推送都走这条路，所以不需要改发布逻辑。
  4. `ctn/pipeline/refresh.py`：新增 `own_metric_step`，与回复候选刷新共用读预算纪律（每轮 ≤3 条、每分钟 ≤9 条、间隔 2s、团队读积压 >120s 时跳过），`refresh_tick` 把两者串在同一个 `refresh` worker（5s）里。
  5. `ops metrics`（`ctn/ops.py`）：打印每条原创最近一次读数 + `trending` 标记 + 年龄，用于对比“推过的”和“没推过的”。
  6. 测试：`tests/test_own_metrics.py`（9 条）覆盖幂等入队、租约、8 个阶段推进到 done、计数镜像、每分钟预算、`ops metrics` 摘要、POST 成功入队 / dry-run 不入队。
- 预期数据（首次部署后）：半天内能拿到每条原创 90s/10min/30min/1h 的点，约 20–30 条原创；能回答“8 次赞 + 7 条回复在 1h 时值多少 hot score，离 trending 阈值差多少”。
- 待办 / 待用户决定：commit + 重启 fleet（需要 `ops backup`）后跑半天；其余步骤 1+2（推送规格 + 带 tag 的转发）、4（回复预算下调）、5（预算保护）按顺序排在后面。

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
