# 实施验证

## 结果

最终Python3.14.6 Linux验证镜像运行 **691条测试全部通过**，包含原有612条按新政策调整的回归及79条新增测试。新增测试覆盖账号初始化、依赖恢复、审核/裁决、Discord真实命令与按钮构建、Pi宏观发送恢复、模式隔离、显式替补计划、容器模式与Pi显式high参数，以及英文单消息面板、菜单、表单和恢复流程。

七项缺陷的回归依据：

- F1：正常同步publisher传入真实RAG快照，通过非Mock命名的合成wire发送，并核对持久化意图中的快照ID。
- F2：真实JevWorkflow→Brain→BotLoop模拟缺上下文，三轮后保持waiting_materials、失败重试消耗为零。
- F3：父帖到达、首次背景材料登记会唤醒；只有重观察时间变化不会唤醒。
- F4：带担忧词的中心问题仍选择P02。
- F5：生产response.v2仅暴露P02/P03/P07/P08，加上盟友独立P01；旧题库保留底层显式接口。
- F6：主服务client和packer使用同一model配置，保留一致性验证。
- F7：保留显式false/unknown，通知标为摘要并补全文，缺失父帖通过共享预算获取。

初始化合成端到端测试确认50个账号都能逐号发完daily，不受旧15条额度影响，也不产生战略原创锁。战略原创仍独立受全队默认10条及单号24小时限制。运行时降额在实际wire检查生效，重启保留。

审核测试确认同版本并发只产生一次批准，重复交互返回相同凭证；批准不授予通道放行；证据变化使旧卡失效；官方拒绝启动停报；重复裁决不重复计入；冲突裁决隔离；24小时未决只提醒一次且不释放pending。

## 数据库

十万/百万合成数据、本机临时文件、WAL/FULL、热缓存。百万数据：

| 查询 | p95毫秒 |
|---|---:|
| 主键查询 | 0.0073 |
| 作者最近20条 | 0.1154 |
| 热任务领取8条 | 0.3842 |
| 运行指标 | 48.4624 |

百万数据主体约852.88MiB；30条新帖加检测器一次事务约3.814毫秒。详细记录见 `benchmark.json`。

活跃批次专项：

| 活跃批次 | 入库30条p50毫秒 | 最后轮SQL条数 |
|---|---:|---:|
| 0 | 2.527 | 722 |
| 50 | 2.496 | 722 |
| 200 | 2.592 | 722 |
| 500 | 2.648 | 722 |

消除了“新帖数×无关活跃批次数”的扫描放大。数据见 `active-batches.json`。这些测量不包含模型、网络或真实平台延迟，不是生产磁盘吞吐保证。

## 容器和真实服务边界

成功构建：

- `ctn2026/verify:single-panel`：固定SDK、核心及Discord依赖，691条离线测试。
- `ctn2026/fleet:local`：实际Compose构建的fleet运行时。
- `ctn2026/discord-review:local`：独立审核UI。
- `ctn2026/macro-agent:local`：固定Pi1.3.0及Node24运行时。

Compose默认fleet与Pi worker、可选Discord profile的配置检查通过。独立临时state与logs目录、`network_mode: none` 下，默认Compose同时启动fleet和worker，二者健康；容器内Pi帮助命令可用、共享SQLite为v6/mock、运行中harness自动创建，发送意图为零。测试容器随后清理。Discord四个命令构建保留前次离线验证，无真实token或Gateway连接。

新增部署脚本检查覆盖：缺少配置时生成模板、再次执行保留现有值、权限600、默认启动fleet+worker、`up all`增加Discord、缺少Discord配置时拒绝启动。对应日志为 `/private/tmp/ctn-docker-ready-verify.log`、`/private/tmp/ctn-docker-ready-build.log`、`/private/tmp/ctn-docker-ready-compose-smoke.log`。

Pi配置跟进：已同步本机DeepSeek凭据与选定默认值到私有Pi挂载目录，宏观镜像及 `.env.macro` 显式固定 `deepseek/deepseek-flash` 与 `high`。新宏观镜像在禁网、默认非root用户下可读取凭据，真实Pi CLI可列出目标模型，环境参数与设置一致；应用镜像目录中不存在真实环境文件或凭据副本，未发起真实模型调用。新增测试核对 `--model`、`--thinking high` 的实际子进程参数、凭据隔离，以及非法thinking在启动Pi前被拒绝。完整测试日志 `/private/tmp/ctn-pi-high-verify.log`，宏观构建日志 `/private/tmp/ctn-pi-high-macro-build.log`。

用户配置后的真实Discord检查已通过：Gateway登录、服务器与文字频道访问、五项所需权限、管理员用户身份及四条guild命令。共享SQLite v6已建立；审核容器单独常驻且健康，举报off，无发送意图或通知队列，未发测试频道消息，也未启动fleet或Pi worker。记录见 `discord-live-check.json`；用户端Slash命令和审核按钮交互仍需在有相应数据时确认。

English UI follow-up: 15 additional workflow tests passed, covering menu-to-candidate navigation, evidence pagination and readable export, permissions on buttons and form submission, stale clicks, manual clearance and suspension, pending-to-ruling confirmation, exact queue counts, embed limits, and persistent-panel restart/timeout recovery. Only the Discord image/service was updated. The requested control panel was published and read back through Discord: five English navigation buttons, no default JSON attachment, no required command arguments, and exactly one main panel in recent channel history. Reporting remains off; no competition action or synthetic production candidate was created. See `discord-panels-live-check.json`; full test/build logs are `/private/tmp/ctn-discord-panels-verify.log` and `/private/tmp/ctn-discord-panels-build.log`. Human Discord clicks were not automated; actual library callbacks were exercised offline against SQLite.

Single-message follow-up: ten additional tests check stable private-message IDs through navigation, evidence, modal submission, confirmation and error handling; repeated launcher reuse; separate reviewer workspaces; attachment cleanup; explicit slash reopen retirement; shared-only refresh; expired-webhook replacement; and idle private-panel cleanup. Full suite: 691/691. Updated only the Discord service. Read-back confirmed the existing shared launcher ID was preserved, exactly one main panel remains, the new in-place routing code is deployed, reporting is off and competition action intents remain zero. See `discord-single-panel-check.json`; test/build logs: `/private/tmp/ctn-single-panel-verify.log` and `/private/tmp/ctn-single-panel-build.log`. Previously created untracked private windows cannot be enumerated through channel history and may need manual dismissal.

真实模型输出、官方SDK返回、限流头与裁决来源需要在部署环境核验。首次上线保留小批账号验证。测试不证明说服效果或比赛得分。
