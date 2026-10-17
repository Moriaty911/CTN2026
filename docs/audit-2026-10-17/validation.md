# 离线验证记录

- 项目基准：`6d8de72`。
- Python：3.14.6；httpx：0.28.1；SQLite：3.53.3。
- 官方 SDK：源码版本 0.5.0，提交 `ffa3a7cceb2a10fa4628ea893b3fb1e41af1260d`。
- 本次使用独立的 `/tmp/ctn-code-audit-20261017` 虚拟环境，以及只读使用的官方 SDK 临时检出 `/tmp/ctn-audit-official-sdk`。临时目录可能随后被系统清理。
- 未修改 requirements.txt、业务代码、skill 或 harness.md。

完整原有测试：

```bash
PYTHONPATH=/tmp/ctn-audit-official-sdk/src \
  /tmp/ctn-code-audit-20261017/bin/python -m unittest discover -s tests
```

结果：`Ran 490 tests in 4.317s`，`OK`。

两个补充脚本：

```bash
PYTHONPATH=/tmp/ctn-audit-official-sdk/src \
  /tmp/ctn-code-audit-20261017/bin/python docs/audit-2026-10-17/verify_findings.py

PYTHONPATH=/tmp/ctn-audit-official-sdk/src \
  /tmp/ctn-code-audit-20261017/bin/python docs/audit-2026-10-17/benchmark_sqlite.py
```

所有补充平台对象都是 fake；所有补充数据库都由 TemporaryDirectory 创建。原测试使用各自的 mock/fake provider。这些验证不提供实盘语义准确率、真实平台网络速度或两周稳定性保证。

`reproductions.json` 和 `benchmark.json` 保留本次输出。另有一次双连接原地 counter 实测：各增加 1000 次，最终值 2000，耗时 25.76 ms，本次未出现锁错误或计数丢失。

依赖对照结果：只安装 requirements.txt 时 9 个测试模块导入失败；增加 PyPI SDK 0.4.0 后共运行 490 个测试，其中 2 个报错；换用上述 SDK 0.5.0 源码后 490 个测试全部通过。
