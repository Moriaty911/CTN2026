# v6 运行手册

本次交付完成合成平台、离线 Discord 交互、本机容器验证，以及用户配置后的真实Discord Gateway连接、频道权限和命令注册检查。Discord审核服务现已单独常驻运行；没有注册真实比赛账号、调用真实模型生成比赛内容，或发出比赛帖子、回复、举报及测试频道消息。

## 状态与默认政策

- 全队阶段：悉尼时间 2026-10-19 **18:00** 启动、10-20 17:00 稳定、11-01 00:00 冲刺。按用户修订的时刻执行，启动期为23小时。
- 注册必须显式执行；注册确认成功后自动安排该账号的 daily initialise。成功一个、放行一个。初始化无旧15条/日限制，不产生24小时原创锁。
- 初始化状态、维护标记、运行状态和能力等级独立。手动停用、冻结、封禁、发送不明不会被冲刺恢复。
- 战略原创默认10条/悉尼日，每个账号战略原创间隔24小时。初始化不占这10条；所有发布仍共享平台限流。模拟不计真实成功或额度。
- 冲刺可豁免尚未开始的初始化；正在发送/结果不明的初始化先对账。退出人工冲刺覆盖后，只有豁免记录的账号重新初始化。
- 同帖最多四个账号回复：首条成功后逐条审核新增贡献，失败不补号。生产用途为P01/P02/P03/P07/P08；其他用途保留底层接口。
- 维护池由人决定。系统报告粉丝变化、可比时长互动、审核样本及当日原创/回复数量；不把互动总数当成NPC或说服分。

## 升级和账号操作

使用项目支持的Python3.14.6与固定SDK。先停止旧fleet、外部Pi cron和所有共享数据库写进程。

```sh
python -m ctn.ops --state-file state/ctn.sqlite3 status
python -m ctn.ops --state-file state/ctn.sqlite3 preflight
python -m ctn.ops --state-file state/ctn.sqlite3 fleet plan --size 50
```

第一条命令建立/迁移v6。本地文件库使用WAL/FULL；已有库迁移前生成 `.before-v6.bak`，进程锁串行迁移。其他进程使用已迁移的库；数据库版本不匹配会拒绝启动。

核对平台开放情况、团队配额、凭证和绑定后，人工执行：

```sh
python -m ctn.ops --state-file state/ctn.sqlite3 fleet register --size 50 --execute
python -m ctn.main --dry-run --state-file state/ctn.sqlite3
```

真实初始化需要显式开启fleet真实写入。先用少量账号验证真实SDK返回与初始化，再完成其余注册。注册返回unknown时，先执行 `fleet check --account HANDLE`，正向登录确认后再继续；失败登录不能证明账号未创建。保留旧账号历史，确认平台名额已释放后，使用未绑定人设创建替补。停用或封禁本身不等于释放平台名额，系统不自动删除账号。

```sh
python -m ctn.ops --state-file state/ctn.sqlite3 fleet plan --size 50 --persona-id yes_080 --account NEW_UNUSED_HANDLE
python -m ctn.ops --state-file state/ctn.sqlite3 fleet register --size 50 --persona-id yes_080 --account NEW_UNUSED_HANDLE --execute
```

将示例中的handle替换为实际小写用户名。已绑定的人设不可换绑；超过目录中可用人设时明确报错，需要另行扩充版本化目录。

本机 `.env` 的账号目标已从40改为50，并声明 `CTN_SIDE=yes`。保持 `CTN_DRY_RUN=true`，举报保持off。不要把模拟成功当成初始化成功。

```sh
python -m ctn.ops --state-file state/ctn.sqlite3 campaign show
python -m ctn.ops --state-file state/ctn.sqlite3 campaign configure --daily-original-limit 12
python -m ctn.ops --state-file state/ctn.sqlite3 phase climax --until '2026-10-30T12:00:00+11:00'
python -m ctn.ops --state-file state/ctn.sqlite3 phase auto
python -m ctn.ops --state-file state/ctn.sqlite3 maintenance recommend
python -m ctn.ops --state-file state/ctn.sqlite3 maintenance admit --account HANDLE --reason '人工核查了可比样本和粉丝变化'
python -m ctn.ops --state-file state/ctn.sqlite3 maintenance release --account HANDLE --reason '持续表现下降，回普通待遇观察'
```

`CTN_DAILY_ORIGINAL_LIMIT=10`仅为新库缺省值；已有库以运行时政策为准。调额不清空已用数量，降到已用数量以下会停止新增战略原创。24小时战略原创间隔保持不变。

`account HANDLE --mode standby` 是人工停用；正向注册检查不会清除这个标记。能力使用已有 `account HANDLE --tier high` 经人工确认配置；职业、粉丝和初始化均不自动授予high。预检列出高能力供应不足。

## 管理式 Pi 原创

独立worker默认两小时一次，先检查额度和可用账号，再收集材料。Pi通过无工具、无扩展的一次性CLI调用返回结构化草稿；Python选账号、绑定快照、审核并执行。失败和未发送不会记成功；发送不明以action_intents为对账依据。模拟与真实宏观任务使用不同任务ID。

```sh
python -m ctn.macro.worker --state-file state/ctn.sqlite3 --once
```

worker跟随fleet的 `CTN_MODE` 选择mock或真实数据源；mock不调用Pi或外部平台。项目live时，宏观发布仍默认只模拟，需另设 `CTN_MACRO_LIVE=true` 或显式加 `--live`；项目本身必须同时满足 `CTN_MODE=live`、`CTN_DRY_RUN=false`。`CTN_PI_BIN`指定Pi可执行文件，`CTN_PI_MODEL`及 `CTN_PI_THINKING` 显式传入Pi；容器与配置模板固定为本机的 `deepseek/deepseek-flash`、`high`，本机未设这些变量时仍用Pi默认配置。模型凭证在部署环境配置，不写入草稿、日志或Git。不要同时运行旧外部cron发布器。

worker从本地harness读取 `[ACTIVE]` 指令，运行摘要由SQLite生成；平台帖子、新闻、人设材料不会被提升为操作员指令。容器使用 `/state/harness.md`，首次从镜像模板建立。

## 举报审核和 Discord

候选批准、人工通道放行、平台接收和官方裁决为四件独立的事。

1. 在fleet的正式配置中选择 `CTN_REPORT_MODE=hybrid`（推荐，默认 shadow：系统只记录建议、仍需人工批准）或 `manual`。off不生成候选。期望值自动决策见 README 的 Reporting automation 一节。
2. 在Discord Developer Portal创建团队应用和Bot，安装到仅队内可见的guild/channel。Scopes为 `bot`、`applications.commands`；授予查看频道、发消息、嵌入链接、附加文件、读取消息历史权限，不授予Administrator。无需Message Content或成员列表特权intent。
3. 复制 `.env.discord.example` 为 `.env.discord`，填写token、guild/channel及管理员/审核员用户或角色ID，权限600。Discord服务只加载此文件。
4. 安装 `requirements-discord.lock`，检查配置后启动：

```sh
python -m pip install -r requirements-discord.lock
python -m ctn.discord_bot --check-config
python -m ctn.discord_bot
```

固定依赖为discord.py2.7.1；无需公网回调端点。所有命令限制在配置的频道，按钮每次重新鉴权。Bot不实例化比赛客户端。

当前实连已通过：Bot `cnt2026` 位于 `CNT2026` 服务器的「常规」频道，所需五项频道权限和配置的管理员用户有效，四条命令已注册。共享SQLite v6已建立，举报off；仅 `discord-review` 容器运行，fleet及Pi worker未启动。可在频道输入 `/reports` 查看空的候选及pending状态。检查记录见 `discord-live-check.json`。后续需要在官方开服前单独启动审核UI时，可在已迁移的库上执行 `docker compose --profile discord up -d --no-deps discord-review`。

- 频道内自动维护英文主面板：Review queue、Pending reports、Reporting controls、Refresh、Help。队列通过菜单选择候选，证据逐条翻页；默认不返回JSON文件。各步骤、按钮结果和表单均有英文说明，详见 `discord-ui.md`。
- Each reviewer uses one private workspace. Navigation, evidence pages, forms, results and error notices edit the same message. Repeated launcher clicks reuse it; the public launcher and teammates' panels remain separate. Idle workspaces close after ten minutes. Reopen once after a Bot update; private windows created by the previous version may need manual dismissal.
- `/reports`：打开带按钮的状态主面板；候选和pending通过按钮查看。
- `/report`：打开候选选择菜单，也保留可选candidate_id参数。证据可在卡片中翻页，或主动下载可读文本。
- 审核员：批准、拒绝、暂缓。批准五分钟有效，暂缓默认三十分钟；拒绝抑制同一证据，更新证据需重新审核。
- Reporting controls中的Confirm availability：管理员填写官方来源和观察时间，人工批准的举报放行60分钟，并解除举报403造成的30分钟自动暂停；不会清除六小时停报。Pause new reports暂停新举报，pending仍保留。`/reporting`无参数也可打开此面板，旧参数形式仍可使用。
- Pending reports中选择一次提交及其官方结果，填写来源和含时区的时间，先预览再确认。`/ruling`无参数也可打开引导列表，旧完整参数调用仍可使用。冲突/错误事件隔离，不覆盖既有结果。

同一次交互重复回放返回同一结果；并发审核一个版本只有一个胜者。发送超时可能产生重复通知卡片，但共用业务状态，不能重复提交举报。Bot离线时outbox保留，CLI仍能审核。

CLI等价入口：

```sh
python -m ctn.ops reports list
python -m ctn.ops reports approve --candidate CANDIDATE_ID --revision REVISION
python -m ctn.ops reports reject --candidate CANDIDATE_ID --reason '证据不足'
python -m ctn.ops reports defer --candidate CANDIDATE_ID --reason '等待更多上下文'
python -m ctn.ops reports clearance --source clearance.json
python -m ctn.ops reports pause
python -m ctn.ops reports calibration
python -m ctn.ops reports automation --shadow off   # 只对 auto_buckets 生效；--shadow on 立即停止自动发送
python -m ctn.ops import-events rulings.json
python -m ctn.ops reports affiliation --source affiliation.json
```

文件格式：

```json
{"allowed":true,"source":"官方举报状态页面及本次人工观察的说明"}
```

```json
[{"event_id":"official-unique-id","attempt_id":"ATTEMPT_ID","subject_kind":"post","subject_id":"POST_ID","outcome":"upheld","ruled_at":1800000000,"source":"官方裁决日志或通知的可追溯来源"}]
```

```json
{"author":"HANDLE","side":"no","team_id":"KNOWN_TEAM_ID","source":"明确的官方阵营/队伍依据","observed_at":1800000000}
```

裁决导入必须绑定attempt或唯一report ID；不是把报告批准当裁决。未知阵营、队伍、举报顺位保持未知；排序为明确NO、未知、明确YES。本队及已验证NPC受保护。账号举报需要已成立隐藏记录，活动量由公开读取的帖子和点赞数自动统计（`api_count:posts+likes`），也可继续用 `account-activity` 人工录入。

全队最多五个pending，同作者一个，且所有pending帖子举报被判genuine的概率之和不超过 `max_expected_genuine`（默认2.0）；超过24小时只提醒一次，不自动超时释放。帖子genuine裁决触发六小时停报；账号dismissed不产生倒补得分。结束时刻可由 `campaign configure --ends-at ISO_TIME` 设置；未确认时界面显示时窗未知。

## 容器、监测与恢复

服务器只需Docker与Compose，不需安装Python、Node、Pi或cron。Pi1.3.0和Node24已包含在macro镜像；worker常驻运行，由SQLite任务租约负责每两小时一次的检查。一次检查最多提出一条草稿，不保证每两小时发布，也不会为补满额度而强行发帖。

首次准备：

```sh
./scripts/docker.sh configure
# 编辑 .env、.env.macro，以及要启用Discord时的 .env.discord
./scripts/docker.sh prepare
./scripts/docker.sh up all
```

`configure`仅创建缺少的配置文件，保留已有值；`prepare`创建挂载目录并设置容器UID10001写权限，Linux上可能需要sudo。`up all`构建并启动fleet、Pi worker与Discord；只需fleet与Pi时使用 `./scripts/docker.sh up`。等价Compose启动命令：

```sh
docker compose --profile discord config --quiet
docker build --target verify -t ctn2026/verify:v6 .
docker compose --profile discord up --build -d
docker compose --profile discord logs -f --tail=100
```

普通 `docker compose up --build -d` 默认启动fleet与Pi worker；Discord保留可选profile，缺少Bot配置时不启动。fleet首先迁移数据库，其他服务等待其健康。原有宏观 `--profile macro` 不再需要。

| 文件或目录 | 用途 | 容器处理 |
|---|---|---|
| `.env` | 平台、JEV、Python生成模型、模式开关 | 注入fleet和worker |
| `.env.macro` | Pi模型、thinking及可选环境凭据 | 仅注入worker |
| `.env.discord` | Bot token、频道与权限ID | 仅注入Discord |
| `config/` 与 `harness.md` 模板 | 人设、语料、策略模板 | 随代码构建入镜像 |
| `state/harness.md` | 可编辑的运行中策略指令 | worker首次创建，后续持久保留 |
| `state/pi/` | 专用Pi配置或登录状态 | 挂载到worker的 `/home/ctn/.pi` |
| `state/` 与 `logs/` | 共享SQLite、发送历史与日志 | 本地磁盘持久化挂载 |

镜像包含可执行程序和配置模板；真实凭据在启动时从文件注入，修改凭据不需重建镜像。`CTN_LLM_API_KEY`不是Pi凭据；Pi需在 `.env.macro` 填其所选provider对应的key，或配置专用 `state/pi`。不要把整份个人Pi目录复制入镜像。

本次已将本机选定的三个默认值及DeepSeek凭据同步到专用 `state/pi/agent/settings.json`、`state/pi/agent/auth.json`，文件权限600；仅复制DeepSeek的认证项。`.env.macro` 固定 `CTN_PI_MODEL=deepseek/deepseek-flash`、`CTN_PI_THINKING=high`，worker显式传入 `--model` 和 `--thinking high`。迁往另一台服务器时，需私密复制这两个Pi配置文件与环境文件，再执行 `prepare` 设置容器访问权限；仅复制镜像不会携带凭据。两个Pi文件及真实环境文件均被Git和Docker构建上下文排除。

模板默认为 `CTN_MODE=mock`、`CTN_DRY_RUN=true`、`CTN_MACRO_LIVE=false`、举报off。演练使用独立state目录；已标记为mock的库拒绝混入真实数据。正式上线使用确认过的真实库/新目录，先显式注册并核验少量账号，然后按运行模式启用fleet与宏观写入；仅启动容器不执行账号注册。切换配置后运行 `up` 或 `up all` 重建容器环境，`restart`不会重载环境变量。

Compose的 `restart: unless-stopped` 负责容器进程退出后的重启；宿主机Docker服务也需设为开机启动。没有额外cron任务。查看全部服务用 `./scripts/docker.sh ps`，查看全部日志用 `./scripts/docker.sh logs all`，停止全部用 `./scripts/docker.sh down`。

fleet heartbeat报告实际队列、初始化、宏观任务、审核状态、业务停滞和磁盘余量；Bot与宏观worker有各自心跳。磁盘空闲不足10%提示。关注unknown发送、失效批准、通道阻断、长期pending和隔离裁决。

## 代码更新与重启（git pull）

在服务器项目目录里按顺序执行。先在旧容器照常运行时建好新镜像，再一次性替换，停机只有几十秒。

```sh
cd <服务器上的项目路径>

# 1. 拉代码。--ff-only：服务器上有人手改过文件时直接报错，不会自动合并
git pull --ff-only
git log --oneline -5

# 2. 离线测试（独立的 verify 镜像，不影响运行中的服务）。不通过就停在这里
./scripts/docker.sh verify

# 3. 备份数据库（服务运行中也可以备份），备份留在 state/ 下
docker compose exec fleet python -m ctn.ops backup /state/backup-$(date +%Y%m%d-%H%M).sqlite3

# 4. 构建新镜像，旧容器继续运行
./scripts/docker.sh build all        # 未启用 Discord：./scripts/docker.sh build

# 5. 替换容器：发送 SIGTERM，最多等 120 秒有序退出
./scripts/docker.sh up all           # 未启用 Discord：./scripts/docker.sh up

# 6. 确认
./scripts/docker.sh ps
./scripts/docker.sh logs all         # 看到 "service started" 后 Ctrl+C
docker compose exec fleet python -m ctn.ops status
```

收到 SIGTERM 后，fleet 立即停止发出新的写请求；还在限速排队的发送判为 failed（`never reached the wire`），目标帖重新排队；已经发出、正在等响应的请求有 20 秒返回。之后容器退出，fleet 先启动并通过健康检查，macro 与 Discord 才会启动。

第 6 步看 `ops status` 末尾的 `unknown` 列表：
- 重启造成的 unknown 应该极少（只有恰好在请求往返的不到 1 秒内被打断的发送）。
- 每条会在 1、3、8、14 分钟自动核对：找到就判成功；两次完整翻页都没找到就判失败、目标帖重新排队；15 分钟仍无法判断就放弃该目标、释放账号。`check_state` 与 `last_check` 显示核对进度。
- 一般不需要人工处理。只有想在 15 分钟内手动收尾时才用 `ops resolve-action`。

不要这样做：
- `docker compose restart` / `./scripts/docker.sh restart`：仍用旧镜像，代码改动不生效，`.env` 也不重新加载。
- `docker kill`、`docker compose down -t 0`、强退 Docker Desktop：进程被直接杀掉。这种情况也能恢复（发送租约 90 秒后，没出过本机的判 failed，出过本机的进入上面的核对），但会多等 90 秒，而且"已发出"的那几条要靠核对收尾。
- 先 `down` 再 `up`：`up` 会逐个替换容器，先 `down` 只会延长停机。

退回上一版本：

```sh
git log --oneline -5
git checkout <上一个正常的提交>
./scripts/docker.sh up all
```

如果新版本已经改了数据库结构、旧代码读不了，需要先停服务，再从第 3 步的备份恢复：

```sh
./scripts/docker.sh down
docker compose run --rm fleet python -m ctn.ops restore /state/backup-<时间>.sqlite3 /state/restored.sqlite3
mkdir -p state/replaced && mv state/ctn.sqlite3* state/replaced/
mv state/restored.sqlite3 state/ctn.sqlite3
./scripts/docker.sh up all
```

恢复旧备份不会撤回平台上已经执行的动作；备份之后发出的内容在恢复后的库里没有记录，重开写入前要对照平台上我方账号的近期发帖，见下一段。

恢复数据库前停掉全部写进程，使用 `ctn.ops backup/restore`。restore写入新路径，不覆盖现有库。恢复旧备份不会撤回平台已执行动作；重开写入前必须对账备份之后的发送记录，防止历史意图丢失导致重发。

官方服务器开放后仍需核对：账号/帖子字段、全文与分页语义、实际配额头、官方裁决来源、high本地配置及平台拒绝行为。离线测试不证明传播效果、真实说服得分或人工裁决可准确识别机器人。
