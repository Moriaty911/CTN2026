# 发帖、回帖、举报逻辑树

按 2026-10-18 当前代码绘制。这里描述**实际实现**，不是建议中的新策略。F/C 编号对应 [审计报告](review.md)。红色是已确认缺陷，黄色是条件性阻断或尚未接通能力。三棵树末端的共享执行校验见文末。

## 1. 发帖逻辑树

战略选题由 `.pi/skills/campaign-publisher/SKILL.md` 规定，触发依赖外部 Pi 调度；Python 不自行执行 A/B 选题。暖号由 Python 负责。

```mermaid
flowchart TD
    A[发帖机会] --> B{来源}
    B -->|Python warmup worker| W{已绑定且 active<br/>尚未成功暖号？}
    W -->|否| END0[不发]
    W -->|是| W1[取该人设 daily seed<br/>purpose=warmup<br/>直接调用异步 publisher]
    B -->|外部 Pi pulse| C[读取 harness 与 SQLite status<br/>排名前20条样本、新闻、配额、账号]
    C --> D{阶段与预算允许战略原创？}
    D -->|否| END1[观察，等待下一 pulse]
    D -->|是| E{样本中的趋势判断}
    E -->|己方占优| FA[branch_A<br/>趋势标签 + 支持己方的标签<br/>具体场景与论证]
    E -->|未知| UF{有依据发布独立论证？}
    UF -->|否| END1
    UF -->|是| FU[branch_unknown<br/>不声称存在多数]
    E -->|对方占优| FB{从上到下扫描<br/>先过滤语言和机器人诱饵}
    FB -->|笼统拒绝/情绪/攻击| F1[B1a/B1b/B1c<br/>可辩护政策理由/回应/共情]
    FB -->|问题/怀疑/反问| F2[B2a/B2b/B2c<br/>作答/温和解释/澄清逻辑]
    FB -->|普通观点| F3{存在可确认的逻辑谬误？}
    F3 -->|是| F31[B3a 针对具体逻辑问题]
    F3 -->|否| F32[B3b 转为独立支持论证]
    FA --> G[选择可用人设<br/>查询绑定 RAG 快照<br/>按人设起草，CLI 发布]
    FU --> G
    F1 --> G
    F2 --> G
    F31 --> G
    F32 --> G
    G --> BUG[F1：publish_sync 未透传 rag_snapshot_id]
    BUG --> H[Publisher 账号/冷却/原帖锁检查<br/>追加标签与280字符等检查]
    W1 --> H
    H --> I{purpose 是 warmup？}
    I -->|是| J[无需战略RAG快照<br/>允许中立，但仍接受语义与证据检查]
    I -->|否| K{有效且用途匹配的RAG快照？}
    K -->|否| BLOCK[blocked<br/>当前正常live CLI会走到这里]
    K -->|是：直接异步调用可达| J2[防复制风格示例<br/>要求明确支持YES]
    J --> L{Jev发前复核通过？}
    J2 --> L
    L -->|否| REJECT[拒绝；Pi提示允许针对原因改写一次]
    L -->|是| M[Actuator原子领取<br/>检查全队每日配额与账号租约<br/>限速后再检查并发送]
    M --> N{实际结果}
    N -->|dry-run| SIM[simulated，不记成功]
    N -->|确认真实post_id| OK[成功落库<br/>账号原帖锁24小时<br/>暖号成功则写warmed_at]
    N -->|结果不确定| UNK[UNKNOWN，占用账号<br/>精确历史正证据对账]
    N -->|确定失败| FAIL[失败，不计成功<br/>按原因处理]
    classDef defect fill:#fee2e2,stroke:#dc2626,color:#7f1d1d;
    classDef pending fill:#fef3c7,stroke:#d97706,color:#78350f;
    class BUG,BLOCK defect;
    class C pending;
```

补充：战略原创配额默认 10/悉尼日，warmup 默认 15/日；两个进程共用发送速率与账号状态。CLI 丢参不应被误读为“战略原创设计上不需要快照”。

## 2. 回帖逻辑树

主路径不自动选择 repost/follow/report。下图的 same/opposing/neutral 针对**当前帖子表达**，不代表作者永久阵营。

```mermaid
flowchart TD
    A[抓取新帖/编辑帖<br/>入库并创建版本任务] --> B[恢复已有回复批次<br/>再领取最多8个新任务]
    B --> C{己方帖子、缺失或机会过期？}
    C -->|是| END0[跳过/过期]
    C -->|否| D{本地语言检查通过？}
    D -->|否| END1[跳过]
    D -->|是| E[Jev triage<br/>风险先于相关性]
    E --> F{相关性与风险}
    F -->|注入/探测/语言风险| REVIEW[needs_review，不发送]
    F -->|明确无关| END2[观察，不互动]
    F -->|相关/尚不明确| G[Jev deep<br/>立场、具体性、机会与职业建议]
    G --> G1{必要上下文或判断缺失？}
    G1 -->|是| CTX[F2：needs_context被映射为needs_assessment<br/>三次后skipped<br/>F3：等待依赖未覆盖父帖到达]
    G1 -->|否| H{当前帖相对YES的立场}
    H -->|同阵营| I{response_specificity > 2.5？}
    I -->|否| LIKE[点赞候选]
    I -->|是| I1{防自回/重复<br/>每小时最多3条<br/>帖子ID哈希约三分之一抽样}
    I1 -->|未通过| LIKE
    I1 -->|通过| P01[P01 支持并增加实质内容]
    H -->|对方| J{reply_value}
    J -->|小于1| END3[不互动]
    J -->|大于3| HIGH[要求high账号]
    J -->|1到3，含端点| K{目标等级映射}
    K -->|high| HIGH
    K -->|unknown| META[C1：等待账号等级<br/>没有自动补齐来源]
    K -->|standard| PURPOSE[本地用途选择]
    HIGH --> PURPOSE
    PURPOSE --> L{按当前代码的顺序}
    L -->|venting| P08[P08 单一澄清问题]
    L -->|担忧关键词| P07[P07 共情并回应<br/>F4：可盖过真正问题]
    L -->|中心question| P02[P02 直接回答]
    L -->|argument且事实/政策<br/>正文无问号| P03[P03 实质论证]
    L -->|其他| P08
    H -->|中立/混合| R[Jev独立response判断<br/>用途与action最高概率≥0.70<br/>差距≥0.15，context足够]
    R --> R1{结果}
    R1 -->|无机会| END4[不互动]
    R1 -->|需材料/审查| WAIT[不生成；当前部分状态会普通重试后跳过]
    R1 -->|P04/P05/P06/P09| UNSUP[F5：当前Brain不承接<br/>unsupported_purpose并跳过]
    R1 -->|P02/P03/P07/P08| ROUTE[统一人设和材料路径]
    P01 --> ROUTE
    P02 --> ROUTE
    P03 --> ROUTE
    P07 --> ROUTE
    P08 --> ROUTE
    ROUTE --> S{已绑定、active、暖号完成<br/>等级合适、非封禁、冷却可用？}
    S -->|否| WAIT2[排队；不换成不合格账号]
    S -->|是| T{绑定MaterialBrief与RAG足够？}
    T -->|否| WAIT3[等待材料；F3依赖不完整]
    T -->|是| U[LLM生成<br/>Jev发前复核＋文本查重<br/>最多定向重写一次]
    U --> V{获批？}
    V -->|否| END5[不发送]
    V -->|是| X{具备持久化批次计划且live？}
    X -->|否| SINGLE[单条执行；dry-run只模拟]
    X -->|是| MULTI{multi_reply_space≥0.90<br/>且材料可用？}
    MULTI -->|否| PLAN1[固定主回复一个槽位]
    MULTI -->|是| PLAN2[固定主回复＋随机1至3个额外候选<br/>不补抽失败候选]
    PLAN1 --> SEND[主回复先发<br/>成功后才允许额外槽位<br/>每条额外回复独立生成并检查新实质内容]
    PLAN2 --> SEND
    SINGLE --> FINAL[最终执行校验与原子结果落库]
    SEND --> FINAL
    FINAL --> RESULT{结果}
    RESULT -->|成功| OK[记真实post_id、冷却、互动、归因与采样]
    RESULT -->|不确定| UNK[UNKNOWN，暂停批次并占用账号<br/>对账后再推进]
    RESULT -->|失败| FAIL[按原因失败/取消/重新排队]
    classDef defect fill:#fee2e2,stroke:#dc2626,color:#7f1d1d;
    classDef pending fill:#fef3c7,stroke:#d97706,color:#78350f;
    class CTX,UNSUP defect;
    class META,WAIT,WAIT2,WAIT3,P07 pending;
```

注意：机会默认 30 分钟，草稿默认 5 分钟；编辑目标会取消旧版本。完整性标记还有 F7：Brain 会把目标和父帖 `text_complete` 强行设为 true，即使上游标记 false。

## 3. 举报逻辑树

`confirmed_bot` 是本地候选标签，**不是官方确认身份**。举报没有 NO 方过滤；其他队伍的 YES 账号也可能进入此树。

```mermaid
flowchart TD
    A[新帖/编辑帖进入检测器] --> B{己方或认证账号？}
    B -->|是| SAFE[保护，不检测/举报]
    B -->|否| C[抽取证据<br/>去除引用/代码示例<br/>按证据键去重，家族封顶]
    C --> C1[泄漏50、重复35、协同35<br/>时序20、弱信号合计最多10<br/>裁决反馈上限10]
    C1 --> D{累计分与实质证据家族}
    D -->|低于50| OBS[保留观察]
    D -->|50以上但不足举报条件| SUS[可疑；可限频拉历史补证据]
    D -->|至少85且至少2个实质家族| READY[本地confirmed_bot候选<br/>分数不是概率]
    READY --> MODE{report_mode与队伍停报状态}
    MODE -->|off或六小时停报中| END0[不提交]
    MODE -->|manual/hybrid可继续| TARGET{已确认动作数≥100<br/>且已有至少1次永久隐藏裁决？}
    TARGET -->|是| ACCT[账号举报候选]
    TARGET -->|否| POST[未报过的帖子候选<br/>必要时限频补抓作者历史]
    ACCT --> PAYLOAD[形成证据版本与哈希<br/>明确inauthentic理由和detail<br/>持久化候选]
    POST --> PAYLOAD
    PAYLOAD --> CLEAR{有效report_clearance？}
    CLEAR -->|否| WAIT0[等待操作员放行或状态源]
    CLEAR -->|是| AUTO{hybrid且自动白名单<br/>且live_source_verified？}
    AUTO -->|是| WHITE[目标存在未引用泄漏<br/>另有至少2个其他帖子/线程<br/>独立重复或协同行为]
    AUTO -->|否| APPROVAL{候选人工批准仍有效？<br/>批准与放行均默认5分钟}
    WHITE --> CAP[提交前最终门控]
    APPROVAL -->|否| WAIT1[等待批准]
    APPROVAL -->|是| CAP
    CAP --> CAP1{全队pending少于5<br/>同作者pending少于1<br/>证据/批准/目标版本仍一致<br/>账号有效、去重与限速通过？}
    CAP1 -->|否| END1[阻断或等待]
    CAP1 -->|是| SEND[提交举报]
    SEND --> RES{提交结果}
    RES -->|确定失败| FAIL[不记裁决成功<br/>403举报拒绝使clearance失效]
    RES -->|发送不确定| UNK[UNKNOWN，保留占用<br/>不能当作未发送而盲重试]
    RES -->|服务器接受| PENDING[pending，只说明已提交]
    PENDING --> SOURCE{有明确且匹配attempt/report的裁决？}
    UNK --> SOURCE
    SOURCE -->|否| NOSOURCE[C2：当前服务UnavailableModerationSource<br/>需人工导入；不可用404代替]
    SOURCE -->|是| RULING{幂等处理裁决}
    RULING -->|upheld| UP[帖子永久隐藏记strike<br/>己方已知strike达3记封禁<br/>账号举报upheld直接记封禁]
    RULING -->|rejected| REJ[全队六小时停报<br/>清空clearance<br/>不把对方认证为NPC]
    RULING -->|dismissed| DIS[不奖分、不触发停报<br/>账号新报需等待与新证据]
    classDef pending fill:#fef3c7,stroke:#d97706,color:#78350f;
    class AUTO,WAIT0,NOSOURCE pending;
```

自动白名单不是任意“85分”就自动报。当前正式装配没有可验证裁决源，人工 clearance 又明确不授权自动模式，因此图中的自动提交分支目前不能通过正常生产入口直接启用。

## 共享最终执行校验

发帖/回复实际发送前再次确认：当前人设目录哈希与版本、绑定账号与激活状态、账号能力与租约、目标版本、草稿/机会期限、RAG 撤回、文本重复、配额和相应策略授权。HTTP 登录、查询和发送也计入共享请求预算。确认成功后，才更新发言计数、冷却与效果归因。

报告编号、精确位置与离线复现见 [审计报告](review.md) 和 [复现结果](reproductions.json)。
