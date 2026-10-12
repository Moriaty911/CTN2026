#!/usr/bin/env python3
"""CTN 2026 环境预检。

用途：
    python scripts/check_env.py            # 只检查解释器
    python scripts/check_env.py --tests    # 检查并跑离线测试

可同时用于：
  · 本地 setup_env.sh
  · CI（push / PR 时先跑这个）
  · 服务器部署前的 preflight
  · 运行时自检（见文末 __main__ 之外的 require_supported_python）

为什么需要硬性版本守卫
----------------------
ctn/jev/inputs.py 的 Optional 解包依赖 `typing.Union is types.UnionType`
这一同一性，而它只在 Python 3.14+ 成立。2026-10 实测同一份代码：

    Python 3.14.8   → 319/319 测试通过
    Python 3.13.13  → 229 failures + 130 errors

代码已修复为兼容两种 union 形式，但守卫保留：那只是我们发现的那一处
差异，未必是唯一一处。比赛期间宁可早失败、失败得响亮。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MINIMUM = (3, 14)


def version_string() -> str:
    return ".".join(str(n) for n in sys.version_info[:3])


def check_python() -> int:
    print(f"  解释器: {sys.executable}")
    print(f"  版本:   {version_string()}")
    if sys.version_info < MINIMUM:
        print(
            f"\n❌ 需要 Python {MINIMUM[0]}.{MINIMUM[1]}+，当前 {version_string()}\n"
            "   原因见 scripts/check_env.py 顶部注释。\n"
            "   修复:  ./scripts/setup_env.sh   （会创建/切换到正确的环境）\n",
            file=sys.stderr,
        )
        return 1
    print("  ✅ 版本符合要求")
    return 0


def run_tests(repo_root: Path) -> int:
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        print("  ⚠ 没有 tests/ 目录，跳过测试")
        return 0
    print("\n  运行离线测试套件 …")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=repo_root,
    )
    if proc.returncode != 0:
        print("\n❌ 测试未通过。不要在此状态下提交或部署。", file=sys.stderr)
    return proc.returncode


def require_supported_python() -> None:
    """供运行时调用：在不支持的解释器上立刻失败，而不是静默算错。

    用法（放在入口模块最顶部）::

        from scripts.check_env import require_supported_python
        require_supported_python()
    """
    if sys.version_info < MINIMUM:
        raise RuntimeError(
            f"CTN 需要 Python {MINIMUM[0]}.{MINIMUM[1]}+，当前 {version_string()}；"
            "请运行 ./scripts/setup_env.sh"
        )


def main(argv: list[str]) -> int:
    print("▸ CTN 环境预检")
    code = check_python()
    if code == 0 and "--tests" in argv:
        code = run_tests(Path(__file__).resolve().parent.parent)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
