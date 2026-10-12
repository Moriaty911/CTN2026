#!/usr/bin/env python3
"""CTN 2026 environment pre-check.

Usage:
    python scripts/check_env.py            # interpreter check only
    python scripts/check_env.py --tests    # check, then run the offline suite

Intended for:
  * local setup via scripts/setup_env.sh
  * CI (run this before anything else on push / PR)
  * server preflight before deployment
  * runtime self-check (see require_supported_python below)

Why a hard version guard is necessary
-------------------------------------
The Optional unwrapping in ctn/jev/inputs.py depends on
`typing.Union is types.UnionType`, which only holds on Python 3.14+.
Measured 2026-10 on the same unchanged code:

    Python 3.14.8   -> 319/319 tests pass
    Python 3.13.13  -> 229 failures + 130 errors

The code has been fixed to accept both union flavours, but the guard stays:
that was the one difference we found, not necessarily the only one. During the
competition it is better to fail early and loudly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MINIMUM = (3, 14)


def version_string() -> str:
    return ".".join(str(n) for n in sys.version_info[:3])


def check_python() -> int:
    print(f"  interpreter: {sys.executable}")
    print(f"  version:     {version_string()}")
    if sys.version_info < MINIMUM:
        print(
            f"\nFAIL: Python {MINIMUM[0]}.{MINIMUM[1]}+ required, found {version_string()}\n"
            "      See the module docstring in scripts/check_env.py for the reason.\n"
            "      Fix:  ./scripts/setup_env.sh   (creates / switches to the right env)\n",
            file=sys.stderr,
        )
        return 1
    print("  OK: version is supported")
    return 0


def run_tests(repo_root: Path) -> int:
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        print("  WARN: no tests/ directory, skipping the test suite")
        return 0
    print("\n  running the offline test suite ...")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=repo_root,
    )
    if proc.returncode != 0:
        print("\nFAIL: tests did not pass. Do not commit or deploy in this state.",
              file=sys.stderr)
    return proc.returncode


def require_supported_python() -> None:
    """Fail immediately on an unsupported interpreter instead of misbehaving.

    Call this from a runtime entry point, e.g. at the top of the main module::

        from scripts.check_env import require_supported_python
        require_supported_python()
    """
    if sys.version_info < MINIMUM:
        raise RuntimeError(
            f"CTN requires Python {MINIMUM[0]}.{MINIMUM[1]}+, found {version_string()}; "
            "run ./scripts/setup_env.sh"
        )


def main(argv: list[str]) -> int:
    print("CTN environment pre-check")
    code = check_python()
    if code == 0 and "--tests" in argv:
        code = run_tests(Path(__file__).resolve().parent.parent)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
