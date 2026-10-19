# 值班日志

新条目加在最上面。格式和规则见 `CLAUDE.md` 第 9 节。不要写入 key、密码或 token。

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
