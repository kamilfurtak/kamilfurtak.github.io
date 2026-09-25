"""Run independent unit, dependency-contract or real-host checks with fail-closed accounting."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
HOST_MODULES = {"test_loader_integration", "test_cli_monitor", "test_cli_startup_host"}
CONTRACT_MODULES = {"test_cli_delivery", "test_activity_http"}
# Real-pi loader checks (managed-context isolation against the installed
# binary). Opt-in gate: they need `pi` on PATH, so they must never join the
# dependency-free unit CI. Run explicitly: scripts/run-tests.py --suite pi
PI_MODULES = {"test_pi_loader_isolation", "test_pi_manager_isolation"}


def suite_for(module: str) -> str:
    if module in HOST_MODULES:
        return "host"
    if module in CONTRACT_MODULES:
        return "contract"
    if module in PI_MODULES:
        return "pi"
    return "unit"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("unit", "contract", "host", "pi"), default="unit")
    args = parser.parse_args()
    modules = sorted((ROOT / "tests").glob("test_*.py"))
    modules = [p for p in modules if suite_for(p.stem) == args.suite]
    total = 0
    failed = []
    with tempfile.TemporaryDirectory(prefix="pi-test-home-") as home:
        env = {**os.environ, "HERMES_HOME": home, "PYTHONDONTWRITEBYTECODE": "1"}
        # Test modules and their child processes import top-level plugin modules.
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(ROOT), os.environ.get("PYTHONPATH", ""))))
        if args.suite == "host":
            env["PI_REQUIRE_HOST_TESTS"] = "1"
        if args.suite == "pi":
            env["PI_REQUIRE_PI"] = "1"
        for module in modules:
            # Process isolation alone does not isolate persistent singleton state.
            env["HERMES_HOME"] = str(Path(home) / module.stem)
            Path(env["HERMES_HOME"]).mkdir()
            print(f"--- {args.suite}: {module.stem}", flush=True)
            try:
                result = subprocess.run([sys.executable, "-m", "unittest", module.stem, "-v"],
                                        cwd=ROOT / "tests", env=env, capture_output=True,
                                        text=True, timeout=300)
            except subprocess.TimeoutExpired:
                print("Module timed out", flush=True)
                failed.append(module.stem)
                continue
            output = result.stdout + result.stderr
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
            counts = re.findall(r"Ran (\d+) tests? in", output)
            count = int(counts[-1]) if counts else 0
            total += count
            if (result.returncode or not count or re.search(r"skipped=\d+", output)
                    or re.search(r"Exception in thread [^\n]+:\nTraceback", output)):
                # unittest otherwise reports OK even when a daemon worker crashes.
                failed.append(module.stem)
    print(f"SUMMARY suite={args.suite} modules={len(modules)} tests={total} failed={failed}")
    return int(bool(failed) or not modules or not total)


if __name__ == "__main__":
    raise SystemExit(main())
