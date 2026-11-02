# 当前三条流程的逻辑树

此文件加入 80 人设、职业路由和多人回复后的执行条件。举报策略保持原样，裁决读取适配器仍未接通；本批只放宽其空账号启动条件，空账号不提交举报。测试通过不代表实盘收益或识别准确率。

## 原创发帖：Pi → 宏观发布器

```mermaid
flowchart TD
    A[Pi 读取 harness 和 macro status] --> B[分别获取热门标签和 ranked 热门帖子\n记录采集时间、来源、样本数、账号状态和请求预算]
    B --> C{样本足以判断局势?}
    C -->|己方占优| D[Pi 选择 branch_A]
    C -->|对方占优| E{优先级一：泛化反对、发泄或攻击?}
    E -->|政策泛化反对，且有可辩护依据| E1[branch_B_1a：回应政策主张]
    E -->|无实质证据的情绪发泄| E2[branch_B_1b：文明回应并回到议题]
    E -->|攻击或辱骂群体| E3[branch_B_1c：反对攻击并表达同理]
    E -->|不匹配| EA{优先级二：问题或疑虑?}
    EA -->|真诚问题| E4[branch_B_2a：有据回答]
    EA -->|质疑己方| E5[branch_B_2b_ours：温和解释]
    EA -->|质疑对方| E6[branch_B_2b_theirs：有据追问]
    EA -->|敌意反问| E7[branch_B_2c：澄清并保持逻辑一致]
    EA -->|不匹配| EB{优先级三：是否存在可指出的谬误?}
    EB -->|是| E8[branch_B_3a：指出具体谬误]
    EB -->|否| E9[branch_B_3b：独立陈述，避免歪曲原意]
    C -->|不足| F[记录 unknown\n观察、warmup 或 branch_unknown]
    D --> G[Pi 从同版本 80 人设库选择已绑定职业与账号、目的、内容和标签]
    E1 --> G
    E2 --> G
    E3 --> G
    E4 --> G
    E5 --> G
    E6 --> G
    E7 --> G
    E8 --> G
    E9 --> G
    F -->|选择发布| G
    F -->|选择观察| F0[记录不足，结束本次发布]
    G --> H{目录版本与阵营一致、已绑定且账号 active\n未封禁冻结、冷却结束、原创锁结束?}
    H -->|否| X[blocked：记录原因]
    H -->|是| I[检查长度、语言、标签、重复正文]
    I --> J{JEV 审核通过?\nwarmup 允许中立，仍审事实、攻击性与注入}
    J -->|否| Y[rejected：定向修正一次或结束]
    J -->|是| K{执行模式}
    K -->|mock 或 dry-run| L[simulated：仅模拟日志\n不占实盘发布额度与冷却]
    K -->|live| M[SQLite 原子领取内容指纹、账号和每日原创额度]
    M --> N[共享 HTTP 预约、登录及路由限速\n真实发送前重查锁、期限和额度]
    N --> O{平台结果}
    O -->|明确成功| P[事务内 succeeded\n计数加一、原创锁 24 小时、写动作日志\n归属人设版本、职业组及指标任务]
    O -->|明确拒绝| Q[failed：释放占位并记录原因]
    O -->|响应丢失或发送中断| R[unknown：保留去重和账号保护\n禁止自动重发]
    R --> S[精确账号历史正向匹配\n或有明确证据的人工处理]
    S --> P
    P --> T[Pi 读取真实 post_id 与状态\n再更新 harness]
```

每日原创默认全队最多 10 条；预约和 unknown 也占相应额度。接到发布命令、领取任务或模拟成功，都不能记作正式发布。

## 自动回帖：同一评估快照与固定批次

```mermaid
flowchart TD
    A[Observed post or comment] --> B[Existing language, JEV action and 0–4 gates]
    B --> C{Reply selected and materials adequate?}
    C -->|No| D[Existing like, skip or defer path]
    C -->|Yes| E[Eligible primary occupation and account]
    E --> F[Draft and review; at most one rewrite]
    F --> G{Approved?}
    G -->|No| H[Cancel or defer with reason]
    G -->|Yes| I{Valid complementary-space judgment ≥ .90?}
    I -->|No| J[Persist one slot]
    I -->|Yes| K[Persist main plus 1–3 compatible fixed candidates]
    J --> L[Claim main slot and reserve account]
    K --> L
    L --> M{Confirmed primary success?}
    M -->|Failed| N[Cancel unsent extras]
    M -->|Unknown| O[Pause batch for reconciliation]
    M -->|Yes| P[Next fixed extra: reuse checked snapshot]
    P --> Q[Generate with successful earlier replies as separate context]
    Q --> R{Supported, substantively new and approved?}
    R -->|No after one rewrite| S[Cancel candidate; no replacement]
    R -->|Yes| T[Recheck version, identity, deadline and shared quota; send]
    T --> U{Result}
    U -->|Unknown| O
    U -->|Confirmed| V[Record publication and schedule metric samples]
    S --> W[Continue remaining fixed slots]
    V --> W
```

职业建议须最高概率至少 0.70、领先至少 0.15；多人空间须有效且 P(Yes) 至少 0.90。缺失或无效的新增判断退回单回复。原 0—4 分动作规则保持不变。

职业可重复，账号不能重复。抽样持久化；额度不足只延期原候选。机会期限 30 分钟、草稿期限 5 分钟；编辑取消未发送部分。追加稿通过材料范围与实质新增检查后才发送，失败不补抽。

## 自动举报：可解释证据 → 有限待审 → 独立裁决事件

```mermaid
flowchart TD
    A[采集帖子及历史，保留帖子、时间、版本和行为引用] --> B[证据去重并按组封顶\n身份泄漏 50、重复 35、跨线程协调 35\n持续时间模式 20、弱特征合计 10\n唯一裁决反馈合计正负 10]
    B --> C{怀疑分与实质证据组}
    C -->|低于 50| D[观察]
    C -->|50 到 84| E[补充历史与上下文]
    C -->|至少 85 且至少两组| F{硬约束通过?}
    F -->|自家、官方 verified、停报中\n已封禁、无可引用材料| G[不提交并记录原因]
    F -->|通过| H{裁决读取能力已验证且自动举报已启用?}
    H -->|否| I[能力未接通：保持观察和离线回放]
    H -->|是| J{已确认隐藏至少一帖\n并有可追溯的至少 100 次账号动作?}
    J -->|是| K[账号举报候选]
    J -->|否| L[未举报帖子候选]
    K --> M[reason 与 detail 分开\nreason=inauthentic，正文列具体证据]
    L --> M
    M --> N[按已确认 team_id 与裁决日评估递减档\n不固定十条后囤积；检查比赛结束时间]
    N --> O[SQLite 发送前去重与待审预约\n默认每目标账号 1 条、全队 10 条]
    O --> P[实际 HTTP 路由额度与 Retry-After\n发送前再次核对保护和停报状态]
    P --> Q{提交结果}
    Q -->|成功| R[pending；仅记提交，不加 strike]
    Q -->|明确失败| G
    Q -->|unknown| S[保留占位，等待明确核对；不重发]
    R --> T[接收带唯一事件 ID、对象、裁决时间与来源的事件]
    T --> U{重复、冲突或早于提交的事件?}
    U -->|是| V[忽略重复或拒绝无效事件]
    U -->|否| W{裁决}
    W -->|upheld| X[裁决日记账，有限正反馈\n帖子累计三次成立才推导封禁]
    W -->|rejected| Y[有限负反馈、六小时停报\n保留身份不确定性]
    W -->|dismissed| Z[记录终态，不加分、不停报]
```

85 分不是 85% 机器人概率。平台裁决不会把目标升级成官方 verified，也不会让相似账号继承身份。404、删除、暂时隐藏和五队举报后的临时隐藏，都不能代替明确裁决。
