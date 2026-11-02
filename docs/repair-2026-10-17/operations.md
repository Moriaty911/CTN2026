# 启动、停机与状态操作

从项目根目录执行。推荐 Python 3.14.6；requirements.txt 固定官方 SDK 0.5.0 提交及下载摘要、OpenAI 客户端和全部传递依赖。Pi 项目 skill 位于 `.pi/skills/campaign-publisher/SKILL.md`，在此项目启动 Pi 时使用；无需拷贝到 Codex 的 skills 目录。项目目录发现规则见 [Pi 官方 skills 仓库说明](https://github.com/badlogic/pi-skills)。在已运行的 Pi 会话执行 `/reload`，再用 `/skill:campaign-publisher` 显式加载；参见 [Pi 官方技能文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md)。

## 安装与模式

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/check_env.py --tests
.venv/bin/python -m ctn.main --mock --side yes --state-file state/mock.sqlite3 --once
.venv/bin/python -m ctn.main --mock --side yes --state-file state/mock.sqlite3
```

`--mock` 使用本地平台和模拟动作，不调用真实 JEV/LLM。`dry-run` 可读取真实平台并调用已配置的模型，所有业务写动作返回 `simulated`。读取所需的真实登录/认证 HTTP 请求仍按实际请求计量。`live` 需要阵营、平台凭证、账号密码、启用的 JEV 和相应 LLM 配置；宏观发布还必须经过审核。

配置优先使用明确 CLI 模式，其次 CTN_MODE；未设置 CTN_MODE 时兼容 CTN_DRY_RUN。冲突的环境模式或 `--mock --live` 会报错。启用 live 时，设置 `CTN_MODE=live` 并删除旧 CTN_DRY_RUN，或同时设为 false。默认自动举报关闭；单独设置 CTN_AUTO_REPORTS=true 不能绕过未接通的裁决能力。

SQLite 路径优先级为：`--state-file`、CTN_STATE_FILE、`CTN_STATE_DIR/ctn.sqlite3`。Pi 与 Python 服务必须指向同一个文件。默认路径首次遇到旧 `.ctn_state.db` 或 `state/ctn.db` 会明确打印迁移来源，保留原文件；有多个旧库则要求明确指定。JSON 不再充当运行数据库。数据源为 mock 的采集库与真实平台库不能混用；模式切换时明确选择对应数据库。相同环境中的 Pi 与 Python 仍共享同一文件。

```bash
.venv/bin/python -m ctn.macro --side yes --mock status
.venv/bin/python -m ctn.macro status --side yes --mock
.venv/bin/python -m ctn.macro --side yes --mock warmup --account bot_worker_yes
.venv/bin/python -m ctn.ops status
```

宏观 CLI 保留两种 --side 位置。`published` 表示真实成功，`simulated` 表示模拟，`unknown` 表示可能已执行但结果尚不确定。只有真实成功后才能更新 harness 的已发布状态。

## 账号与材料

新库从 harness 种入默认规则：worker active、debater standby、medic/policy dormant；原创后锁 24 小时，全队每日最多 10 条原创。账号能力等级是本地明确配置，不代表官方 verified 身份。配置和状态在 SQLite 中，改 harness 表格不会更改执行约束。

在战略阶段和人工指令允许后，才应用对应状态，例如：

```bash
.venv/bin/python -m ctn.ops account bot_medic_yes --mode active --tier high
.venv/bin/python -m ctn.ops account bot_medic_yes --mode dormant
```

需要事实依据的回复只使用经过可信注册的材料，目标帖不能充当答案依据。经人工核对的材料 JSON 格式为：

```json
[{"reference_id":"unique-reference","text":"Verified supplied statement.","source_id":"the actual document or URL"}]
```

```bash
.venv/bin/python -m ctn.ops background /absolute/path/references.json
```

重启服务后应用该材料注册。材料不足会延期，P08 可使用不依赖外部事实的澄清目的。不能通过编造政策豁免、数字或来源填补空缺。

## 运行与停机

```bash
docker compose build fleet
docker compose up -d fleet
docker compose logs -f fleet
docker compose stop fleet
```

容器默认执行 `python -m ctn.main`。挂载目录按 compose 约定授予 UID/GID 10001 写权限。进程收到 SIGTERM/SIGINT 后停止调度，取消未结束逻辑任务，发送中断记为 unknown；容器提供 120 秒退出宽限，允许底层网络线程结束。强制断电/杀进程后的发送状态通过持久化租约恢复；发送中失去持有者不会自动重发。

`CTN_STATE_DIR/heartbeat` 记录存活、错误、各 worker 最近开始/完成时间、队列积压及年龄、待审数、unknown 数和真实请求数。业务积压长期不推进会标记 business_stalled；健康检查检查心跳新鲜度与进程错误，空闲不等于故障。各阶段耗时与拒绝理由也写入 logs 下的遥测和数据库任务记录。

## 一致性备份与恢复

```bash
.venv/bin/python -m ctn.ops --state-file /absolute/path/ctn.sqlite3 backup /absolute/path/backups/state-20261017.sqlite3
.venv/bin/python -m ctn.ops restore /absolute/path/backups/state-20261017.sqlite3 /absolute/path/restored.sqlite3
```

备份使用 SQLite backup API，不能只复制运行中的主文件而漏掉 WAL。恢复要求目标文件不存在，先做 integrity_check；切换到恢复库之前停止 Python 服务和 Pi 的发布操作，再统一修改两者的路径。数据库、迁移前备份和凭证文件按 0600 创建或限制权限；备份也含账号会话，应按凭证保管。

旧库升级前保存 `.before-v3.bak`。升级事务失败会回滚，备份可恢复；若同名备份已存在，保留它并改用新的备份位置或恢复到新文件，不覆盖证据。旧版身份评分会归档到 legacy_bot_profiles 并重新累计。旧动作日志无法可靠区分真实发送和旧版 dry-run，因此转为 unknown，先核对历史再继续相关账号。

旧 JSON 账号状态可显式导入：

```bash
.venv/bin/python -m ctn.ops --state-file /absolute/path/ctn.sqlite3 import-accounts /absolute/path/legacy.json
```

## 结果未知与裁决

服务用已确认的账号历史读取接口核对原创/回帖：作者、文本、时间及回复对象必须精确匹配，且只能有一条正向匹配。查不到不等于未发送；点赞、举报等未确认核对能力的动作继续保持 unknown。

人工确有证据时可使用 `resolve-action ACTION_ID evidence.json`。成功凭据必须提供 source、reason、outcome=succeeded 和 response.post_id；确认从未发送才可设置 outcome=failed 与 confirmed_not_sent=true。工具保留整份处理依据。不得仅凭有限历史中查不到记录就判定失败并重发。

裁决事件内部契约：

```json
[{"event_id":"unique-official-event","subject_kind":"post","subject_id":"actual-post-id","outcome":"upheld","ruled_at":1792220000,"source":"actual-verified-source"}]
```

subject_kind 可为 post/account；outcome 可为 upheld/rejected/dismissed。提交与裁决时间、事件来源均保存。明确核对后的事件可以通过 `ctn.ops import-events /absolute/path/events.json` 事务导入。模拟来源和真实来源必须区分。账号举报还需明确来源的动作计数，不能拿采集到的帖子数冒充至少 100 次平台动作。

## 周一仍须完成的真实验证

2026-10-19 平台开放后，使用官方资料及正常登录页面确认裁决来源、字段、稳定事件 ID、认证、分页与裁决时间。以真实样本实现 ModerationSource，补齐适配器契约测试和影子运行，再启用自动提交。当前正式服务始终装配 UnavailableModerationSource，没有预设裁决 URL。比赛结束时间也应在核实后写入 campaign_ends_at；未知时不伪造时间。
