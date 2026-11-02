# CNT2026 v5：运行、RAG和混合举报

本次实现只经过离线验证，没有注册账号、实盘发布、举报或上云。80个人设保留；平台账号分批30→50，未绑定的人设不参与调度。比赛是否开放以真实平台响应为准，不按本地时钟自动开启live。

## 环境与启动

生产和CI使用Dockerfile固定的Python 3.14.6及requirements中锁定的SDK 0.5.0提交。`pypi-capturethenarrative/`只是本地参考，不进入镜像或生产依赖。

Pi和Python必须使用同一个本地SQLite文件，禁止把SQLite放在NFS或运行水平副本。单机多进程通过短事务共享动作、账号、额度和文本指纹。凭证通过环境注入，数据库及备份限制为0600。

```bash
python -m ctn.ops --state-file /state/ctn.sqlite3 preflight
python -m ctn.main --mock --side yes --state-file /tmp/cnt-mock.sqlite3 --once
```

mock与真实数据使用不同数据库。配置保持`CTN_SIDE=yes`、`CTN_MODE=dry-run`、`CTN_DRY_RUN=true`；启动实盘时必须明确使用live并提供平台、JEV及LLM配置。preflight分别显示离线、发布和举报就绪程度，始终不输出凭证。

`SIGTERM`/`Ctrl-C`停止服务，等待当前任务清理。发送中断进入unknown。心跳包含实际业务完成时间、worker错误、RAG版本、队列和举报阻塞原因；依赖调用失败不伪装成业务完成。

## 账号与warmup

```bash
python -m ctn.ops fleet plan --size 30
python -m ctn.ops fleet plan --size 50
python -m ctn.ops fleet import --source /secure/accounts.json
python -m ctn.ops persona activate --group transport_logistics
```

导入文件为数组，记录含`persona_id`、`account_id`及可选`password`、`token`、`platform_account_id`。导入后默认standby，不给已有发布身份换人设。职业不授予high等级。

注册命令是独立运维入口，只有显式`fleet register --size 30 --execute`才会注册；本批交付没有运行该命令。失败或响应丢失会停止批次。sending/unknown不得重新注册，先用`fleet check --account <handle>`以正向登录证据核对；登录失败不能证明平台没有创建账号。

首批30的职业数为4/4/4/4/3/3/3/3，加2个写作者。50阶段前六个普通职业组各6、农业现场及办公室社区各5、写作者2、医生1、法律1；warmup阶段保留医生和法律席位，实际最多48，不擅自启用受限角色。平台已注册但本地standby的账号仍占平台席位；被封账号也不能假定能删除。

Python warmup worker对新账号发一条中立日常内容，走同一个publisher和审核。确认成功后登记readiness；普通冷却结束才能回帖，原创24小时锁仍有效。Pi不另起warmup调度。战略原创10条／悉尼日，warmup单独15条／日；claimed、sending、unknown和成功均占各自额度，实际HTTP预算共享。

## RAG和Pi发布

正式静态库是`config/rag/yes.v1.1.json`：32条事实／归属说法、21张论点卡、22项未知、30条风格示例。检索采用词法、主题及领域词表，不使用问题白名单或预写答案；两报纸／VIP持续采集和FTS5未实现。

```bash
python -m ctn.rag query --purpose P01 --text "proof checks on public transport" --json
python -m ctn.macro --side yes publish --account <bound-handle> \
  --purpose P01 --branch branch_unknown --rag-snapshot <snapshot-id> \
  --text "<reviewed English draft>" --dry-run
```

新增来源通过归档接口完整、按版本留存；现有用户原始MD会保留一致性副本。官方设定目前保留来源URL、章节与审阅日期，本批没有新增全文抓取器。原子主张保留来源位置、归属和时效；相同主张可合并多个来源，不能把转载算独立证据。新闻只能作为attributed_claim，不能变成official_fact。档案接口和精确主张合并已预留，尚未自动抓取外部新闻。

JEV判断前读取相关的typed材料，不再固定选第一条背景。snapshot ID绑定语料哈希、检索策略、目标版本、材料和用途；传给生成与审核的是实际评估的材料。风格最多一条、仅供语气参考，人设优先。原文、原始审阅段落、完整语料不会整批进入生成。

Pi实盘战略发布必须提供数据库中的snapshot ID；publisher重新读取并检查，拒绝缺失、用途不符、已撤回或目标已编辑的材料。普通语料更新不作废已固定且有效的批次。撤回用`ctn.ops rag-withdraw <snapshot-id>`，或`rag-withdraw --reference F006 --corpus-hash <hash>`撤回该版本材料涉及的全部快照；尚未发送的相关动作会被阻止。

全文重复在共享事务中拒绝；近似措辞和风格复制允许一次定向重写，仍不合格则取消。必要事实可复用，不能为差异化编造信息。postflight.v3独立检查事实支持；追加还必须有实质新增内容。P08只能围绕目标中的真实信息缺口提问。

缺少材料或账号等级的任务进入waiting_materials，不反复调用模型；语料／相应元数据变化后恢复，原30分钟机会期限仍有效。生成草稿5分钟有效。单个依赖错误有限重试，磁盘、数据库或配置错误停止写入。

## 混合举报

`CTN_REPORT_MODE`为`off`、`manual`或`hybrid`，默认off。旧`CTN_AUTO_REPORTS`不能开启新的举报通道。版本化白名单见`policies/report-v1.json`。

85分、两个实质证据组只是候选门槛，不是概率。hybrid自动要求目标帖的非引用模型／模板泄漏，加上至少两个其他帖子、至少两个其他线程中的独立重复或协调证据。重复泄漏、热门口号、时间或语言特征、相似账号标签和裁决反馈不能代替独立行为。

```bash
python -m ctn.ops reports list
python -m ctn.ops reports clearance --source /secure/current-report-state.json
python -m ctn.ops reports approve --candidate <candidate-id>
python -m ctn.ops import-events /secure/verified-rulings.json
python -m ctn.ops account-activity /secure/confirmed-activity.json
```

人工状态文件至少包含`{"source":"实际核查位置与时间说明","allowed":true}`，表示操作者刚刚确认当前可举报，有效期5分钟。它不能开启自动通道。人工批准也只有5分钟，绑定目标、类型、详情、证据哈希，单次使用；发送前再次检查。

真实裁决／停报读取接口尚未验证，因此当前生产装配的自动举报保持blocked。后续适配器必须确认实时来源及其有效期；不能根据404、暂时隐藏或普通403猜裁决和停报。manual也需要明确当前状态与逐条批准。

全队最多5条待审，每目标账号最多1条，sending和unknown也占位。自动／人工共享保护、格式、额度和停报检查。账号举报另需可靠的100次动作及至少一条正式裁决隐藏帖。确认计数文件包含`account_id`、`action_count`、`observed_at`（epoch秒）与`source`；不能拿已抓取帖子数冒充平台动作数；dismissed保留历史，须等待且有新证据后才可再次提交。

裁决事件含`event_id`、`subject_kind`、`subject_id`、`outcome`、`ruled_at`、`source`；多次账号举报还必须提供`attempt_id`或可唯一匹配的`report_id`。事件逐条校验、隔离无效数据，重复不再处罚。迟到事件不能覆盖另一次举报的状态。明确裁决可证明unknown举报已经提交并解除其占位；裁决结果不改变官方身份认证。

## 备份、恢复和unknown

```bash
python -m ctn.ops --state-file /state/ctn.sqlite3 backup /backups/ctn-20261018.sqlite3
python -m ctn.ops restore /backups/ctn-20261018.sqlite3 /restore/ctn.sqlite3
python -m ctn.ops --state-file /restore/ctn.sqlite3 status
```

v4→v5迁移前自动创建一致性备份。失败保留原库；已有备份不能被覆盖。恢复目的文件必须不存在，并通过integrity_check。恢复只用于演练或确认没有真实动作的场景；实盘后禁止用旧库抹掉新动作账本，应暂停写入并向前修复。

unknown发帖／回帖只通过准确匹配的账号历史，或明确人工证据解决。`resolve-action`成功证明必须有真实post_id；失败证明必须明确`confirmed_not_sent=true`。历史列表中没找到不构成失败证明。不要靠换账号、换slot或重启绕过去重。

## 验证边界

见`validation.json`记录离线测试、容器及同机性能对照。52个检索案例检查必要事实、未知项和限定词；它们不证明真实模型的生成质量、举报准确率或比赛收益。正式启用须先验证真实平台字段、少量账号与恢复流程，再扩至30／50。
