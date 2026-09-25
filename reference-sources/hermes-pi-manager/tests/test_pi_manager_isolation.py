"""Manager-level managed-context isolation against the REAL pi binary.

Full path: manager -> probe (cold cache) -> spawn, with a wrapper pi that
records every executed argv, a synthetic agent dir carrying an executable
marker extension, and a local fake provider endpoint. Asserts that:
  - the compatibility probe never executes personal resources (a raw
    ``pi --help`` in the same environment DOES fire the marker — control);
  - neither the probe nor the RPC spawn fires the marker extension;
  - the executed argv carries the isolation flags, the empty --system-prompt
    sentinel and the policy TEXT (not a path);
  - the captured provider request contains no planted markers;
  - manager recovery spawns with the same isolation contract.

Needs a real ``pi`` (PI_BIN or PATH). Opt-in gate: scripts/run-tests.py
--suite pi. Never touches the real agent dir or production credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

import rpc_transport  # noqa: E402
from core import PiManager, Thresholds  # noqa: E402
from registry_db import Registry  # noqa: E402
from rpc_transport import MANAGED_CONTEXT_FLAGS  # noqa: E402
from test_pi_loader_isolation import (  # noqa: E402
    EXTENSION_MARKER,
    _CaptureHandler,
    payload_text,
    write,
)

PI_BIN = os.environ.get("PI_BIN") or shutil.which("pi")

E2E_THRESHOLDS = Thresholds(
    heartbeat_seconds=30.0,
    rpc_timeout_seconds=20.0,
    waiting_seconds=30.0,
    soft_stall_seconds=120.0,
    tool_stall_seconds=600.0,
    stall_grace_seconds=30.0,
    emergency_cap_seconds=1800.0,
    terminate_grace_seconds=2.0,
    watchdog_interval_seconds=999999.0,
)


def wait_until(predicate, timeout: float = 60.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


WRAPPER = '''#!/usr/bin/env python3
"""Executes the real pi, recording each invocation's argv first."""
import json
import os
import sys
log = os.environ.get("PI_ARGV_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
os.execv(@@REAL@@, [@@REAL@@] + sys.argv[1:])
'''


def build_synth_agent(tmp: Path, port: int) -> Path:
    agent = tmp / "agent"
    write(agent / "settings.json", json.dumps({
        "defaultProvider": "fake", "defaultModel": "fake-model",
    }))
    write(agent / "models.json", json.dumps({"providers": {"fake": {
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "api": "openai-completions",
        "apiKey": "test-key",
        "models": [{
            "id": "fake-model", "name": "Fake Model", "reasoning": False,
            "input": ["text"], "contextWindow": 8192, "maxTokens": 1024,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }],
    }}}))
    write(agent / "extensions" / "iso-marker.ts", EXTENSION_MARKER)
    return agent


def write_wrapper(tmp: Path) -> Path:
    path = tmp / "pi-wrapper.py"
    path.write_text(WRAPPER.replace("@@REAL@@", repr(PI_BIN)), encoding="utf-8")
    path.chmod(0o755)
    return path


class ManagerIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PI_BIN:
            raise unittest.SkipTest("pi binary not available")
        cls.tmp = Path(tempfile.mkdtemp(prefix="pi-manager-isolation-"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        cls.server.captured = []  # type: ignore[attr-defined]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self, agent: Path, marker: Path, argv_log: Path, wrapper: Path) -> dict:
        return {
            "PI_BIN": str(wrapper),
            "PI_ARGV_LOG": str(argv_log),
            "PI_CODING_AGENT_DIR": str(agent),
            "PI_TEST_EXTENSION_MARKER": str(marker),
        }

    def test_probe_never_executes_personal_extensions(self):
        marker = self.tmp / "probe-ext-fired"
        agent = build_synth_agent(self.tmp / "probe", self.port)
        env = {"PI_CODING_AGENT_DIR": str(agent), "PI_TEST_EXTENSION_MARKER": str(marker)}
        # Control: a raw --help in this environment DOES execute the marker
        # extension (pi builds runtime/resources before printing help) — this
        # is the defect the isolated probe exists to avoid.
        (self.tmp / "probe").mkdir(exist_ok=True)
        subprocess.run([PI_BIN, "--help"], capture_output=True, text=True, timeout=90,
                       cwd=str(self.tmp), env={**os.environ, **env})
        self.assertTrue(marker.exists(),
                        "control run failed: raw --help did not fire the extension")
        marker.unlink()
        rpc_transport._SUPPORT_CACHE.clear()
        with mock.patch.dict(os.environ, env, clear=False):
            ok, detail = rpc_transport.pi_isolation_support(PI_BIN)
        self.assertTrue(ok, detail)
        self.assertFalse(marker.exists(),
                         "the compatibility probe executed a personal extension")

    def test_cold_cache_manager_probe_and_spawn_do_not_execute_extensions(self):
        base = self.tmp / "cold"
        base.mkdir()
        marker = base / "mgr-ext-fired"
        argv_log = base / "argv.jsonl"
        wrapper = write_wrapper(base)
        agent = build_synth_agent(base, self.port)
        registry = Registry(db_path=base / "registry.sqlite3")
        manager = PiManager(
            registry=registry, clock=time.time, default_thresholds=E2E_THRESHOLDS,
            executor_profiles={"synthetic": {"provider": "fake", "model": "fake-model",
                                             "thinking": "off"}},
            default_executor="synthetic",
        )
        task_id = None
        try:
            rpc_transport._SUPPORT_CACHE.clear()  # cold cache on purpose
            with mock.patch.dict(os.environ, self.env(agent, marker, argv_log, wrapper)):
                task_id = manager.start_task(prompt="reply OK", cwd=str(base))["task_id"]
                self.assertTrue(wait_until(
                    lambda: manager.status(task_id)["execution_state"] in ("SETTLED", "CRASHED"),
                    timeout=120.0), "task never reached a terminal state")
            row = registry.get_task(task_id)
            self.assertEqual(row["execution_state"], "SETTLED",
                             f"task failed: {row.get('last_error')}")
            self.assertFalse(marker.exists(),
                             "an extension executed during the probe or the RPC spawn")
            lines = [json.loads(line) for line in argv_log.read_text().splitlines()]
            rpc_calls = [a for a in lines if "--mode" in a]
            self.assertEqual(len(rpc_calls), 1, lines)
            argv = rpc_calls[0]
            for flag in MANAGED_CONTEXT_FLAGS:
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--system-prompt") + 1], "")
            policy_path = Path(argv[argv.index("--append-system-prompt") + 1])
            self.assertTrue(policy_path.is_absolute())
            policy = policy_path.read_text(encoding="utf-8")
            self.assertIn("leaf worker in the Hermes", policy)
            probes = [a for a in lines if "--mode" not in a]
            self.assertTrue(any("--version" in a for a in probes), probes)
            self.assertTrue(any("--no-extensions" in a and "--help" in a for a in probes),
                            f"probe help invocation must carry --no-extensions: {probes}")
            captured = [payload_text(r) for r in self.server.captured]  # type: ignore[attr-defined]
            self.assertTrue(captured, "no provider request was captured")
            for text in captured:
                self.assertIn("leaf worker in the Hermes", text)
                self.assertNotIn("iso_marker_tool", text)
                self.assertNotIn("MARKER_", text)
        finally:
            rt = manager._rt(task_id) if task_id else None
            if rt is not None and getattr(rt, "transport", None) is not None:
                rt.transport.terminate()
            manager.shutdown()
            registry.close()

    def test_manager_recovery_uses_isolated_argv(self):
        base = self.tmp / "recovery"
        base.mkdir()
        marker = base / "mgr-ext-fired"
        argv_log = base / "argv.jsonl"
        wrapper = write_wrapper(base)
        agent = build_synth_agent(base, self.port)
        registry = Registry(db_path=base / "registry.sqlite3")
        manager = PiManager(
            registry=registry, clock=time.time, default_thresholds=E2E_THRESHOLDS,
            executor_profiles={"synthetic": {"provider": "fake", "model": "fake-model",
                                             "thinking": "off"}},
            default_executor="synthetic",
        )
        first_id = None
        seed_id = "pi-seed-recovery"
        try:
            rpc_transport._SUPPORT_CACHE.clear()
            with mock.patch.dict(os.environ, self.env(agent, marker, argv_log, wrapper)):
                first_id = manager.start_task(prompt="reply OK", cwd=str(base))["task_id"]
                self.assertTrue(wait_until(
                    lambda: manager.status(first_id)["execution_state"] == "SETTLED",
                    timeout=120.0), "seed task never settled")
                first = registry.get_task(first_id)
                self.assertFalse(marker.exists())
                before = len(argv_log.read_text().splitlines())
                registry.create_task(
                    task_id=seed_id, cwd=str(base),
                    executor_profile="synthetic",
                    executor_spec=json.dumps({"provider": "fake", "model": "fake-model",
                                              "thinking": "off"}),
                    execution_state="RUNNING", verification_state="NOT_RUN",
                    session_id=first["session_id"], session_file=first["session_file"],
                    expected_session_id=first["session_id"],
                    expected_session_file=first["session_file"],
                )
                result = manager.recover_task(seed_id)
                self.assertTrue(result.get("recovered"), result)
                self.assertFalse(marker.exists(), "recovery executed the extension")
                lines = [json.loads(line) for line in argv_log.read_text().splitlines()]
                recovery_calls = [a for a in lines[before:] if "--mode" in a]
                self.assertEqual(len(recovery_calls), 1, lines[before:])
                argv = recovery_calls[0]
                for flag in MANAGED_CONTEXT_FLAGS:
                    self.assertIn(flag, argv)
                self.assertEqual(argv[argv.index("--system-prompt") + 1], "")
                self.assertIn("leaf worker in the Hermes",
                              Path(argv[argv.index("--append-system-prompt") + 1])
                              .read_text(encoding="utf-8"))
                self.assertEqual(argv[argv.index("--session") + 1], first["session_file"])
                self.assertEqual(registry.get_task(seed_id)["execution_state"], "WAITING")
        finally:
            for task in (first_id, seed_id):
                rt = manager._rt(task) if task else None
                if rt is not None and getattr(rt, "transport", None) is not None:
                    rt.transport.terminate()
            manager.shutdown()
            registry.close()


if __name__ == "__main__":
    unittest.main()
