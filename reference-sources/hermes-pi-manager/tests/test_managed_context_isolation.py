"""Managed-context isolation tests (unit level).

Covers the argv contract (isolation flags + suppressed SYSTEM.md + the policy
TEXT appended exactly once), the fail-closed policy loading matrix (missing,
empty, whitespace-only, invalid UTF-8, directory, unreadable, relative-path
base, replacement/removal after validation — for BOTH start and recovery), the
pi compatibility probe contract (isolated per-process environment, non-zero
--version exit, unparseable version, missing flags, unreadable help) and a
real-process end-to-end run of the manager against a fake RPC pi binary.

The REAL pi loader is exercised by the opt-in ``pi`` suite
(``tests/test_pi_loader_isolation.py``, ``tests/test_pi_manager_isolation.py``).

Run: python3 -m unittest -v test_managed_context_isolation (from tests/)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

from fakes import FakeClock, FakePiProcess  # noqa: E402
from core import EXEC_CRASHED, EXEC_RUNNING, MAX_POLICY_BYTES, PiManager, Thresholds  # noqa: E402
from registry_db import Registry  # noqa: E402
from rpc_transport import (  # noqa: E402
    MANAGED_CONTEXT_FLAGS,
    MANAGED_ISOLATION_ID,
    build_rpc_argv,
    _SUPPORT_CACHE,
    pi_isolation_support,
)


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


TEST_THRESHOLDS = Thresholds(
    heartbeat_seconds=20.0,
    rpc_timeout_seconds=5.0,
    waiting_seconds=10.0,
    soft_stall_seconds=90.0,
    tool_stall_seconds=300.0,
    stall_grace_seconds=15.0,
    emergency_cap_seconds=900.0,
    terminate_grace_seconds=0.3,
    watchdog_interval_seconds=999999.0,
)


def sha16(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def execution_lock_path(registry, task_id: str) -> Path:
    return registry.path.parent / "execution-locks" / (hashlib.sha256(task_id.encode()).hexdigest() + ".lock")


def lock_is_free(path: Path) -> bool:
    import fcntl
    if not path.exists():
        return True
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


GOOD_HELP = """Usage: pi [options]

  --system-prompt <text>         System prompt (default: coding assistant prompt)
  --append-system-prompt <text>  Append text or file contents to the system prompt
  --no-extensions, -ne           Disable extension discovery
  --no-skills, -ns               Disable skills discovery and loading
  --no-prompt-templates, -np     Disable prompt template discovery and loading
  --no-context-files, -nc        Disable AGENTS.md and CLAUDE.md discovery and loading
  --mode <mode>                  text, json or rpc
"""

# Probe double: handles the exact probe invocation shapes (--version, and
# --no-extensions --help), records its own argv/agent-dir/cwd when asked to,
# touches a runtime marker if it is ever called as a runtime, else exits 3.
PROBE_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys
argv = sys.argv[1:]
log = os.environ.get("PI_PROBE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": argv,
                             "agent": os.environ.get("PI_CODING_AGENT_DIR"),
                             "cwd": os.getcwd()}) + "\\n")
if "--version" in argv:
    print(@@VERSION@@)
    sys.exit(@@VERSION_EXIT@@)
if "--help" in argv:
    print(@@HELP@@)
    sys.exit(@@HELP_EXIT@@)
if "--mode" in argv:
    open(@@MARKER@@, "w").write("runtime-called")
sys.exit(3)
"""

FAKE_PI_SCRIPT = '''#!/usr/bin/env python3
"""Fake pi binary: help/version for the probe, minimal RPC for the manager."""
import json
import os
import sys
import time

argv = sys.argv[1:]
if "--version" in argv:
    print("0.86.0")
    sys.exit(0)
if "--help" in argv:
    print("""\\
  --system-prompt <text>         System prompt (default: coding assistant prompt)
  --append-system-prompt <text>  Append text or file contents to the system prompt
  --no-extensions, -ne           Disable extension discovery
  --no-skills, -ns               Disable skills discovery and loading
  --no-prompt-templates, -np     Disable prompt template discovery and loading
  --no-context-files, -nc        Disable AGENTS.md and CLAUDE.md discovery and loading
  --mode <mode>                  text, json or rpc
""")
    sys.exit(0)

if "--mode" not in argv or argv[argv.index("--mode") + 1] != "rpc":
    sys.exit(2)

argv_file = os.environ.get("PI_FAKE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w") as fh:
        json.dump(argv, fh)

session_file = None
if "--session" in argv:
    session_file = argv[argv.index("--session") + 1]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


settled = False
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except ValueError:
        continue
    ctype = cmd.get("type")
    cid = cmd.get("id")
    if ctype == "get_state":
        send({"type": "response", "command": "get_state", "id": cid, "success": True,
              "data": {"sessionId": "sess-fake-e2e", "sessionFile": session_file,
                        "isStreaming": False, "isCompacting": False,
                        "messageCount": 1, "pendingMessageCount": 0}})
    elif ctype == "get_entries":
        send({"type": "response", "command": "get_entries", "id": cid, "success": True,
              "data": {"entries": [], "leafId": None}})
    elif ctype == "prompt":
        send({"type": "response", "command": "prompt", "id": cid, "success": True, "data": {}})
        if not settled:
            settled = True
            send({"type": "agent_start"})
            time.sleep(0.05)
            send({"type": "message_end", "message": {"role": "assistant", "content": "done"}})
            time.sleep(0.05)
            send({"type": "agent_settled"})
            time.sleep(2.0)
            sys.exit(0)
    else:
        send({"type": "response", "command": ctype, "id": cid, "success": True, "data": {}})
'''


def write_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def probe_script(tmp: Path, name: str, **kw) -> Path:
    defaults = dict(VERSION=json.dumps("0.86.0"), VERSION_EXIT="0",
                    HELP=json.dumps(GOOD_HELP), HELP_EXIT="0",
                    MARKER=json.dumps(str(tmp / (name + "-runtime"))))
    defaults.update(kw)
    body = PROBE_SCRIPT
    for key, value in defaults.items():
        body = body.replace(f"@@{key}@@", value)
    return write_script(tmp / f"{name}.py", body)


class IsolationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pi-isolation-test-"))
        self.cwd_dir = self.tmp / "cwd"
        self.cwd_dir.mkdir()
        self.registry = Registry(db_path=self.tmp / "registry.sqlite3")
        self.clock = FakeClock()

    def tearDown(self):
        self.registry.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# argv contract
# ---------------------------------------------------------------------------


class BuildRpcArgvTests(unittest.TestCase):
    def test_isolation_flags_sentinel_and_policy_text(self):
        policy_text = "POLICY-LINE-1\nPOLICY-LINE-2\n"
        argv = build_rpc_argv(
            "pi", provider="prov", model="mod", thinking="high",
            session_file="/s/session.jsonl", append_system_prompt=policy_text,
        )
        for flag in MANAGED_CONTEXT_FLAGS:
            self.assertIn(flag, argv)
        # Explicit "no SYSTEM.md": an empty --system-prompt value that also
        # keeps the default base preamble.
        idx = argv.index("--system-prompt")
        self.assertEqual(argv[idx + 1], "")
        append = argv.index("--append-system-prompt")
        self.assertEqual(argv[append + 1], policy_text,
                         "the builder passes the caller's value through unchanged "
                         "(core passes a manager-owned snapshot path here)")
        self.assertEqual(argv.count("--append-system-prompt"), 1, "policy must apply once")
        self.assertEqual(argv[argv.index("--provider") + 1], "prov")
        self.assertEqual(argv[argv.index("--model") + 1], "mod")
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertEqual(argv[argv.index("--session") + 1], "/s/session.jsonl")

    def test_flags_present_for_every_session_variant(self):
        for kwargs in (dict(no_session=True), dict(session_id="sid"), {}):
            argv = build_rpc_argv("pi", provider="p", model="m", thinking="low", **kwargs)
            for flag in MANAGED_CONTEXT_FLAGS:
                self.assertIn(flag, argv, kwargs)
            self.assertEqual(argv[argv.index("--system-prompt") + 1], "", kwargs)
            self.assertNotIn("--append-system-prompt", argv, kwargs)


# ---------------------------------------------------------------------------
# fail-closed policy loading
# ---------------------------------------------------------------------------


class PolicyLoadingTests(IsolationTestCase):
    def make_manager(self, factory):
        manager = PiManager(registry=self.registry, popen_factory=factory,
                            clock=self.clock, default_thresholds=TEST_THRESHOLDS)
        self.addCleanup(manager.shutdown)
        return manager

    def recording_manager(self, calls):
        def factory(argv, cwd):
            calls.append((list(argv), cwd))
            return FakePiProcess()
        return self.make_manager(factory)

    def assert_failed_without_spawn(self, manager, calls, fragment, **start_kw):
        task_id = manager.start_task(prompt="do the thing", cwd=str(self.cwd_dir),
                                     **start_kw)["task_id"]
        self.assertTrue(wait_until(
            lambda: manager.status(task_id)["execution_state"] == EXEC_CRASHED),
            "task never reached CRASHED")
        self.assertEqual(calls, [], "spawn must not be attempted for a bad policy")
        row = self.registry.get_task(task_id)
        self.assertIn(fragment, row["last_error"])
        self.assertTrue(wait_until(lambda: task_id not in manager._execution_locks),
                        "execution ownership was never released")
        self.assertNotIn(task_id, manager._execution_locks)
        self.assertTrue(lock_is_free(execution_lock_path(self.registry, task_id)),
                        "execution ownership must be released after the failure")
        self.assertFalse(manager._rt(task_id).boot_pending,
                         "boot_pending must be cleared after the failed boot")
        self.assertNotIn(self.registry.get_task(task_id)["wake_state"],
                         ("pending", "dispatching", "accepted"),
                         "a rejected policy must never schedule a wake")
        return task_id

    def test_missing_default_policy(self):
        calls = []
        manager = self.recording_manager(calls)
        with mock.patch("core.DEFAULT_SYSTEM_PROMPT_FILE", self.tmp / "gone" / "pi-worker.md"):
            self.assert_failed_without_spawn(manager, calls, "policy file is missing")

    def test_missing_override_policy(self):
        calls = []
        manager = self.recording_manager(calls)
        self.assert_failed_without_spawn(
            manager, calls, "policy file is missing",
            system_prompt_file=str(self.tmp / "nope.md"))

    def test_empty_policy(self):
        calls = []
        policy = self.tmp / "empty.md"
        policy.write_bytes(b"")
        manager = self.recording_manager(calls)
        self.assert_failed_without_spawn(manager, calls, "policy file is empty",
                                         system_prompt_file=str(policy))

    def test_whitespace_only_policy(self):
        calls = []
        policy = self.tmp / "blank.md"
        policy.write_text("  \n\t\n", encoding="utf-8")
        manager = self.recording_manager(calls)
        self.assert_failed_without_spawn(manager, calls, "no non-whitespace content",
                                         system_prompt_file=str(policy))

    def test_invalid_utf8_policy(self):
        calls = []
        policy = self.tmp / "bad.md"
        policy.write_bytes(b"valid start \xff\xfe broken")
        manager = self.recording_manager(calls)
        self.assert_failed_without_spawn(manager, calls, "not valid UTF-8",
                                         system_prompt_file=str(policy))

    def test_directory_policy(self):
        calls = []
        policy = self.tmp / "policydir"
        policy.mkdir()
        manager = self.recording_manager(calls)
        self.assert_failed_without_spawn(manager, calls, "is a directory",
                                         system_prompt_file=str(policy))

    def test_unreadable_policy(self):
        calls = []
        policy = self.tmp / "locked.md"
        policy.write_text("rules", encoding="utf-8")
        os.chmod(policy, 0o000)
        try:
            manager = self.recording_manager(calls)
            self.assert_failed_without_spawn(manager, calls, "unreadable",
                                             system_prompt_file=str(policy))
        finally:
            os.chmod(policy, 0o600)

    def test_default_policy_delivered_as_text_with_matching_hash(self):
        calls = []
        process = FakePiProcess()

        def factory(argv, cwd):
            calls.append((list(argv), cwd))
            process.sync_session_state_from_argv(argv)
            return process

        manager = self.make_manager(factory)
        task_id = manager.start_task(prompt="work", cwd=str(self.cwd_dir))["task_id"]
        self.assertTrue(wait_until(lambda: manager.status(task_id)["execution_state"] == EXEC_RUNNING))
        argv = calls[0][0]
        delivered = Path(argv[argv.index("--append-system-prompt") + 1])
        raw = (PLUGIN_DIR / "pi-worker.md").read_bytes()
        self.assertTrue(delivered.is_absolute(),
                        "pi must receive an absolute snapshot path")
        self.assertEqual(delivered.parent, self.registry.path.parent / "policy-snapshots")
        self.assertEqual(delivered.read_bytes(), raw,
                         "the snapshot path must carry exactly the validated bytes")
        iso = [e for e in self.registry.recent_events(task_id, 50)
               if e["event_type"] == "isolation_applied"]
        self.assertEqual(len(iso), 1)
        summary = json.loads(iso[0]["summary"])
        self.assertEqual(summary["id"], MANAGED_ISOLATION_ID)
        self.assertEqual(summary["policy"]["sha256"], sha16(raw))
        self.assertEqual(summary["policy"]["source"], str(PLUGIN_DIR / "pi-worker.md"))
        self.assertEqual(summary["policy"]["delivery"], "snapshot-path")
        self.assertEqual(summary["policy"]["snapshot"], str(delivered))
        self.assertEqual(summary["pi"]["version"], "test-double")

    def test_override_wins_over_default(self):
        calls = []
        override = self.tmp / "override.md"
        override.write_text("OVERRIDE-POLICY\n", encoding="utf-8")
        manager = self.recording_manager(calls)
        task_id = manager.start_task(prompt="work", cwd=str(self.cwd_dir),
                                     system_prompt_file=str(override))["task_id"]
        self.assertTrue(wait_until(lambda: bool(calls)))
        argv = calls[0][0]
        delivered = Path(argv[argv.index("--append-system-prompt") + 1])
        self.assertEqual(delivered.read_text(encoding="utf-8"), "OVERRIDE-POLICY\n")
        iso = [e for e in self.registry.recent_events(task_id, 50)
               if e["event_type"] == "isolation_applied"][0]
        summary = json.loads(iso["summary"])
        self.assertEqual(summary["policy"]["sha256"], sha16(b"OVERRIDE-POLICY\n"))

    def test_relative_override_resolves_against_manager_cwd(self):
        manager_base = self.tmp / "manager-base"
        manager_base.mkdir()
        (manager_base / "rel.md").write_text("MANAGER-BASE-POLICY\n", encoding="utf-8")
        (self.cwd_dir / "rel.md").write_text("TASK-CWD-POLICY\n", encoding="utf-8")
        calls = []
        manager = self.recording_manager(calls)
        # The patch must stay active until the boot thread has resolved the
        # path: resolution happens at spawn time, in that thread.
        with mock.patch("os.getcwd", return_value=str(manager_base)):
            manager.start_task(prompt="work", cwd=str(self.cwd_dir),
                               system_prompt_file="rel.md")
            self.assertTrue(wait_until(lambda: bool(calls)), "spawn never happened")
        argv = calls[0][0]
        delivered = Path(argv[argv.index("--append-system-prompt") + 1])
        self.assertEqual(delivered.read_text(encoding="utf-8"),
                         "MANAGER-BASE-POLICY\n",
                         "a relative override resolves against the MANAGER cwd, "
                         "never the task cwd")

    def test_replacement_and_removal_after_validation_cannot_change_delivery(self):
        calls = []
        policy = self.tmp / "hot.md"
        original = "ORIGINAL-POLICY-BYTES\n"
        policy.write_text(original, encoding="utf-8")
        process = FakePiProcess()

        def factory(argv, cwd):
            # The window between validation and spawn: the source file is
            # rewritten AND deleted before a child would have read it.
            policy.write_text("REPLACED\n", encoding="utf-8")
            policy.unlink()
            calls.append((list(argv), cwd))
            process.sync_session_state_from_argv(argv)
            return process

        manager = self.make_manager(factory)
        task_id = manager.start_task(prompt="work", cwd=str(self.cwd_dir),
                                     system_prompt_file=str(policy))["task_id"]
        self.assertTrue(wait_until(lambda: manager.status(task_id)["execution_state"] == EXEC_RUNNING))
        argv = calls[0][0]
        delivered = Path(argv[argv.index("--append-system-prompt") + 1])
        self.assertEqual(delivered.read_text(encoding="utf-8"), original,
                         "source replacement+removal must not change the snapshot")
        iso = [e for e in self.registry.recent_events(task_id, 50)
               if e["event_type"] == "isolation_applied"][0]
        self.assertEqual(json.loads(iso["summary"])["policy"]["sha256"],
                         sha16(original.encode("utf-8")))

    def test_recovery_uses_the_same_validated_policy(self):
        session_file = self.tmp / "recover.jsonl"
        session_file.write_text('{"id": "seed"}\n', encoding="utf-8")
        task_id = "task-recover-policy"
        self.registry.create_task(
            task_id=task_id, cwd=str(self.cwd_dir),
            execution_state="RUNNING", verification_state="NOT_RUN",
            session_id="sess-r", session_file=str(session_file),
            expected_session_id="sess-r", expected_session_file=str(session_file),
        )
        calls = []
        process = FakePiProcess()
        process.state.update({
            "sessionId": "sess-r", "sessionFile": str(session_file),
            "isStreaming": False, "isCompacting": False, "messageCount": 1,
            "pendingMessageCount": 0,
        })

        def factory(argv, cwd):
            calls.append((list(argv), cwd))
            process.sync_session_state_from_argv(argv)
            return process

        manager = self.make_manager(factory)
        result = manager.recover_task(task_id)
        self.assertTrue(result.get("recovered"), result)
        argv = calls[0][0]
        raw = (PLUGIN_DIR / "pi-worker.md").read_bytes()
        delivered = Path(argv[argv.index("--append-system-prompt") + 1])
        self.assertEqual(delivered.read_bytes(), raw)
        for flag in MANAGED_CONTEXT_FLAGS:
            self.assertIn(flag, argv)
        iso = [e for e in self.registry.recent_events(task_id, 80)
               if e["event_type"] == "isolation_applied"]
        self.assertEqual(len(iso), 1, "recovery must record its own isolation evidence")

    def test_recovery_with_bad_policy_is_rejected_before_spawn(self):
        session_file = self.tmp / "recover-bad.jsonl"
        session_file.write_text('{"id": "seed"}\n', encoding="utf-8")
        task_id = "task-recover-bad-policy"
        self.registry.create_task(
            task_id=task_id, cwd=str(self.cwd_dir),
            execution_state="RUNNING", verification_state="NOT_RUN",
            session_id="sess-b", session_file=str(session_file),
            expected_session_id="sess-b", expected_session_file=str(session_file),
        )
        calls = []
        manager = self.recording_manager(calls)
        with mock.patch("core.DEFAULT_SYSTEM_PROMPT_FILE", self.tmp / "gone.md"):
            result = manager.recover_task(task_id)
        self.assertFalse(result.get("recovered"))
        self.assertIn("policy", result.get("reason", ""))
        self.assertEqual(calls, [])
        self.assertTrue(lock_is_free(execution_lock_path(self.registry, task_id)))


# ---------------------------------------------------------------------------
# pi compatibility probe / gate
# ---------------------------------------------------------------------------


class PolicyTypeGuards(IsolationTestCase):
    """R1: the fd that is actually opened decides, not a path precheck."""

    def make_manager(self, factory):
        return PiManager(registry=self.registry, popen_factory=factory,
                         clock=self.clock, default_thresholds=TEST_THRESHOLDS)

    def assert_failed_without_spawn(self, manager, calls, fragment, **start_kw):
        task_id = manager.start_task(prompt="do the thing", cwd=str(self.cwd_dir),
                                     **start_kw)["task_id"]
        self.assertTrue(wait_until(
            lambda: manager.status(task_id)["execution_state"] == EXEC_CRASHED),
            "task never reached CRASHED")
        self.assertEqual(calls, [], "spawn must not be attempted for a bad policy")
        row = self.registry.get_task(task_id)
        self.assertIn(fragment, row["last_error"])
        self.assertTrue(wait_until(lambda: task_id not in manager._execution_locks),
                        "execution ownership was never released")
        self.assertFalse(manager._rt(task_id).boot_pending)
        self.assertNotIn(self.registry.get_task(task_id)["wake_state"],
                         ("pending", "dispatching", "accepted"),
                         "a rejected policy must never schedule a wake")
        return task_id

    def test_fifo_with_available_data_is_rejected_and_never_read(self):
        """A held reader keeps written bytes in the FIFO while the manager
        tries to load it: rejection must come from the fd type, with data
        available and a writer process alive, never from an actual read."""
        fifo = self.tmp / "fifo-with-writer"
        os.mkfifo(fifo)
        holder = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        wrote = threading.Event()
        try:
            def writer():
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                except OSError:
                    return
                try:
                    os.write(fd, b"FIFO-POLICY-MARKER")
                    wrote.set()
                finally:
                    os.close(fd)

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            self.assertTrue(wrote.wait(2.0),
                            "the fixture writer never managed to put data in the FIFO")
            thread.join(timeout=3)
            calls: list = []

            def factory(argv, cwd):
                calls.append(argv)
                return FakePiProcess()

            manager = self.make_manager(factory)
            self.addCleanup(manager.shutdown)
            self.assert_failed_without_spawn(manager, calls, "not a regular file",
                                             system_prompt_file=str(fifo))
        finally:
            os.close(holder)

    def test_fifo_without_writer_fails_fast_without_blocking(self):
        fifo = self.tmp / "fifo-no-writer"
        os.mkfifo(fifo)
        calls: list = []

        def factory(argv, cwd):
            calls.append(argv)
            return FakePiProcess()

        manager = self.make_manager(factory)
        self.addCleanup(manager.shutdown)
        started = time.time()
        self.assert_failed_without_spawn(manager, calls, "not a regular file",
                                         system_prompt_file=str(fifo))
        self.assertLess(time.time() - started, 10.0,
                        "a writer-less FIFO must never block the boot path")

    def test_symlink_to_regular_policy_is_allowed(self):
        target = self.tmp / "real-policy.md"
        target.write_text("SYMLINK-POLICY\n", encoding="utf-8")
        link = self.tmp / "link-policy.md"
        link.symlink_to(target)
        calls: list = []

        def factory(argv, cwd):
            calls.append((list(argv), cwd))
            return FakePiProcess()

        manager = self.make_manager(factory)
        self.addCleanup(manager.shutdown)
        task_id = manager.start_task(prompt="work", cwd=str(self.cwd_dir),
                                     system_prompt_file=str(link))["task_id"]
        self.assertTrue(wait_until(lambda: bool(calls)), "symlink policy never spawned")
        delivered = Path(calls[0][0][calls[0][0].index("--append-system-prompt") + 1])
        self.assertEqual(delivered.read_text(encoding="utf-8"), "SYMLINK-POLICY\n")

    def test_delivered_path_is_an_absolute_snapshot_immune_to_decoy(self):
        policy = self.tmp / "short.md"
        policy.write_text("Be concise.\n", encoding="utf-8")
        (self.cwd_dir / "Be concise.").write_text("DECOY-CONTENT\n", encoding="utf-8")
        calls: list = []

        def factory(argv, cwd):
            calls.append((list(argv), cwd))
            return FakePiProcess()

        manager = self.make_manager(factory)
        self.addCleanup(manager.shutdown)
        manager.start_task(prompt="work", cwd=str(self.cwd_dir),
                           system_prompt_file=str(policy))
        self.assertTrue(wait_until(lambda: bool(calls)))
        delivered = Path(calls[0][0][calls[0][0].index("--append-system-prompt") + 1])
        self.assertTrue(delivered.is_absolute(),
                        "the child must never receive cwd-relative, ambiguous input")
        self.assertEqual(delivered.read_text(encoding="utf-8"), "Be concise.\n")
        self.assertEqual(delivered.parent, self.registry.path.parent / "policy-snapshots")
        self.assertNotEqual(delivered.read_text(encoding="utf-8"), "DECOY-CONTENT\n")


class SnapshotIntegrityTests(IsolationTestCase):
    """Existing-snapshot handling: missing / valid / corrupt / special /
    oversize / unreadable / concurrent first creation / recovery."""

    def make_manager(self, factory):
        return PiManager(registry=self.registry, popen_factory=factory,
                         clock=self.clock, default_thresholds=TEST_THRESHOLDS)

    def snapshot_root(self) -> Path:
        return self.registry.path.parent / "policy-snapshots"

    def snapshot_path(self, raw: bytes) -> Path:
        return self.snapshot_root() / (hashlib.sha256(raw).hexdigest() + ".md")

    def packaged_policy(self) -> bytes:
        return (PLUGIN_DIR / "pi-worker.md").read_bytes()

    def recording_factory(self, calls):
        def factory(argv, cwd):
            calls.append((list(argv), cwd))
            return FakePiProcess()
        return factory

    def assert_start_failed_without_spawn(self, manager, calls, fragment):
        task_id = manager.start_task(prompt="work", cwd=str(self.cwd_dir))["task_id"]
        self.assertTrue(wait_until(
            lambda: manager.status(task_id)["execution_state"] == EXEC_CRASHED),
            "task never reached CRASHED")
        self.assertEqual(calls, [], "spawn must not be attempted")
        row = self.registry.get_task(task_id)
        self.assertIn(fragment, row["last_error"])
        self.assertTrue(wait_until(lambda: task_id not in manager._execution_locks),
                        "execution ownership was never released")
        self.assertFalse(manager._rt(task_id).boot_pending)
        self.assertNotIn(self.registry.get_task(task_id)["wake_state"],
                         ("pending", "dispatching", "accepted"))
        return task_id

    def test_missing_snapshot_is_created_private_and_task_starts(self):
        calls = []
        manager = self.make_manager(self.recording_factory(calls))
        self.addCleanup(manager.shutdown)
        task_id = manager.start_task(prompt="work", cwd=str(self.cwd_dir))["task_id"]
        self.assertTrue(wait_until(lambda: bool(calls)), "task never spawned")
        argv = calls[0][0]
        delivered = Path(argv[argv.index("--append-system-prompt") + 1])
        raw = self.packaged_policy()
        self.assertEqual(delivered, self.snapshot_path(raw))
        self.assertEqual(delivered.read_bytes(), raw)
        self.assertEqual(delivered.stat().st_mode & 0o777, 0o600)

    def test_valid_existing_snapshot_is_reused_without_rewrite(self):
        raw = self.packaged_policy()
        root = self.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        target = self.snapshot_path(raw)
        target.write_bytes(raw)
        os.chmod(target, 0o600)
        before = target.stat()
        calls = []
        manager = self.make_manager(self.recording_factory(calls))
        self.addCleanup(manager.shutdown)
        manager.start_task(prompt="work", cwd=str(self.cwd_dir))
        self.assertTrue(wait_until(lambda: bool(calls)))
        after = target.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns),
                         (before.st_ino, before.st_mtime_ns),
                         "an existing valid snapshot must be reused, never rewritten")
        self.assertEqual(target.read_bytes(), raw)

    def test_corrupt_existing_snapshot_fails_closed_and_stays_untouched(self):
        raw = self.packaged_policy()
        root = self.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        target = self.snapshot_path(raw)
        target.write_bytes(b"CORRUPTED-PRE-EXISTING\n")
        os.chmod(target, 0o600)
        before = target.stat()
        calls = []
        manager = self.make_manager(self.recording_factory(calls))
        self.addCleanup(manager.shutdown)
        self.assert_start_failed_without_spawn(manager, calls, "snapshot")
        self.assertEqual(target.read_bytes(), b"CORRUPTED-PRE-EXISTING\n",
                         "the invalid snapshot must not be overwritten or repaired")
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)

    def test_fifo_at_snapshot_path_is_rejected_within_bounds(self):
        raw = self.packaged_policy()
        root = self.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        target = self.snapshot_path(raw)
        os.mkfifo(target)
        calls = []
        manager = self.make_manager(self.recording_factory(calls))
        self.addCleanup(manager.shutdown)
        started = time.time()
        self.assert_start_failed_without_spawn(manager, calls, "not a regular file")
        self.assertLess(time.time() - started, 12.0,
                        "a FIFO at the snapshot path must never stall the boot path")

    def test_oversize_existing_snapshot_fails_closed(self):
        raw = self.packaged_policy()
        root = self.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        target = self.snapshot_path(raw)
        target.write_bytes(b"X" * (MAX_POLICY_BYTES + 4096))
        os.chmod(target, 0o600)
        before = target.stat()
        calls = []
        manager = self.make_manager(self.recording_factory(calls))
        self.addCleanup(manager.shutdown)
        self.assert_start_failed_without_spawn(manager, calls, "too large")
        self.assertEqual(target.stat().st_size, before.st_size)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)

    def test_unreadable_existing_snapshot_fails_closed(self):
        raw = self.packaged_policy()
        root = self.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        target = self.snapshot_path(raw)
        target.write_bytes(raw)
        os.chmod(target, 0o000)
        try:
            calls = []
            manager = self.make_manager(self.recording_factory(calls))
            self.addCleanup(manager.shutdown)
            if os.geteuid() == 0:
                # root ignores the permission bits: the snapshot is valid and
                # must be reused, not rejected (documented root behaviour).
                manager.start_task(prompt="work", cwd=str(self.cwd_dir))
                self.assertTrue(wait_until(lambda: bool(calls)))
            else:
                self.assert_start_failed_without_spawn(manager, calls, "unreadable")
        finally:
            os.chmod(target, 0o600)

    def test_two_concurrent_first_creations_share_one_verified_snapshot(self):
        raw = b"CONCURRENT-SNAPSHOT-PAYLOAD\n"
        digest = hashlib.sha256(raw).hexdigest()
        registry2 = Registry(db_path=self.tmp / "registry-2.sqlite3")
        self.addCleanup(registry2.close)
        managers = [PiManager(registry=self.registry), PiManager(registry=registry2)]
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run(manager):
            try:
                barrier.wait(timeout=10)
                results.append(manager._ensure_policy_snapshot(
                    raw, digest, "concurrent", Path("/concurrent/source.md")))
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(item,), daemon=True)
                   for item in managers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2, "both creators must resolve the snapshot")
        self.assertEqual(results[0], results[1])
        target = Path(results[0])
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        leftovers = list(target.parent.glob(f".{digest}.*.tmp"))
        self.assertEqual(leftovers, [], "temp files must be cleaned up on every path")

    def test_concurrent_creations_never_overwrite_an_invalid_existing_snapshot(self):
        raw = b"CONCURRENT-INVALID-PAYLOAD\n"
        digest = hashlib.sha256(raw).hexdigest()
        root = self.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{digest}.md"
        target.write_bytes(b"BAD-PRE-EXISTING\n")
        registry2 = Registry(db_path=self.tmp / "registry-2.sqlite3")
        self.addCleanup(registry2.close)
        managers = [PiManager(registry=self.registry), PiManager(registry=registry2)]
        barrier = threading.Barrier(2)
        errors = []

        def run(manager):
            try:
                barrier.wait(timeout=10)
                manager._ensure_policy_snapshot(raw, digest, "concurrent", Path("/x"))
            except RuntimeError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=run, args=(item,), daemon=True)
                   for item in managers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(len(errors), 2,
                         "both creators must refuse an invalid existing address")
        self.assertEqual(target.read_bytes(), b"BAD-PRE-EXISTING\n",
                         "the invalid snapshot must survive every concurrent path")

    def test_recovery_fails_closed_on_corrupt_snapshot_and_leaves_it(self):
        raw = self.packaged_policy()
        root = self.snapshot_root()
        root.mkdir(parents=True, exist_ok=True)
        target = self.snapshot_path(raw)
        target.write_bytes(b"CORRUPT-RECOVERY\n")
        os.chmod(target, 0o600)
        session_file = self.tmp / "recover-snap.jsonl"
        session_file.write_text('{"id": "seed"}\n', encoding="utf-8")
        task_id = "task-recover-snap"
        self.registry.create_task(
            task_id=task_id, cwd=str(self.cwd_dir),
            execution_state="RUNNING", verification_state="NOT_RUN",
            session_id="sess-snap", session_file=str(session_file),
            expected_session_id="sess-snap", expected_session_file=str(session_file),
        )
        calls = []
        manager = self.make_manager(self.recording_factory(calls))
        self.addCleanup(manager.shutdown)
        result = manager.recover_task(task_id)
        self.assertFalse(result.get("recovered"), result)
        reason = str(result.get("reason") or result)
        self.assertIn("recovery_spawn_failed", reason)
        self.assertIn("snapshot", reason)
        self.assertEqual(calls, [])
        self.assertEqual(target.read_bytes(), b"CORRUPT-RECOVERY\n")
        self.assertTrue(wait_until(lambda: task_id not in manager._execution_locks))


class ProbeTests(IsolationTestCase):
    def test_supported_binary_reports_version_and_is_cached(self):
        script = probe_script(self.tmp, "pi-good")
        ok, detail = pi_isolation_support(str(script))
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "0.86.0")
        self.assertIs(pi_isolation_support(str(script)), pi_isolation_support(str(script)))

    def test_probe_runs_isolated_and_never_touches_inherited_agent_dir(self):
        marker_dir = self.tmp / "personal-agent"
        marker_dir.mkdir()
        (marker_dir / "settings.json").write_text("{}", encoding="utf-8")
        log = self.tmp / "probe-log.jsonl"
        script = probe_script(self.tmp, "pi-env")
        with mock.patch.dict(os.environ, {
                "PI_CODING_AGENT_DIR": str(marker_dir),
                "PI_PROBE_LOG": str(log)}):
            ok, detail = pi_isolation_support(str(script))
        self.assertTrue(ok, detail)
        lines = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(lines), 2, "expected the version and help probes")
        # Help probe carries the explicit --no-extensions guard.
        help_line = [line for line in lines if "--help" in line["argv"]][0]
        self.assertIn("--no-extensions", help_line["argv"])
        for line in lines:
            self.assertNotEqual(line["agent"], str(marker_dir),
                                "probe must not use the inherited agent dir")
            self.assertIn("pi-isolation-probe-", line["agent"] or "")
            self.assertNotEqual(line["cwd"], os.getcwd(),
                                "probe must not run in the inherited cwd")
        # The inherited (personal) directory is untouched by the probe.
        self.assertEqual(sorted(p.name for p in marker_dir.iterdir()), ["settings.json"])

    def test_version_nonzero_exit_fails_closed(self):
        script = probe_script(self.tmp, "pi-vex", VERSION_EXIT="3")
        ok, detail = pi_isolation_support(str(script))
        self.assertFalse(ok)
        self.assertIn("--version exited 3", detail)

    def test_unparseable_version_fails_closed(self):
        script = probe_script(self.tmp, "pi-badver", VERSION=json.dumps("banana"))
        ok, detail = pi_isolation_support(str(script))
        self.assertFalse(ok)
        self.assertIn("unparseable version", detail)

    def test_help_nonzero_exit_fails_closed(self):
        script = probe_script(self.tmp, "pi-hex", HELP_EXIT="2")
        ok, detail = pi_isolation_support(str(script))
        self.assertFalse(ok)
        self.assertIn("--help exited 2", detail)

    def test_missing_flags_are_named(self):
        script = probe_script(self.tmp, "pi-noflags", HELP=json.dumps("no flags here\n"))
        ok, detail = pi_isolation_support(str(script))
        self.assertFalse(ok)
        self.assertIn("--no-context-files", detail)

    def test_unexecutable_binary_fails_closed(self):
        ok, detail = pi_isolation_support(str(self.tmp / "does-not-exist.py"))
        self.assertFalse(ok)
        self.assertIn("cannot execute", detail)

    def test_spawn_refused_before_process_start_on_unsupported_binary(self):
        marker = self.tmp / "runtime-called"
        script = probe_script(self.tmp, "pi-nofly",
                              VERSION=json.dumps("0.70.0"),
                              HELP=json.dumps("no flags here\n"),
                              MARKER=json.dumps(str(marker)))
        manager = PiManager(registry=self.registry, clock=self.clock,
                            default_thresholds=TEST_THRESHOLDS)  # REAL factory on purpose
        self.addCleanup(manager.shutdown)
        with mock.patch.dict(os.environ, {"PI_BIN": str(script)}):
            task_id = manager.start_task(prompt="x", cwd=str(self.cwd_dir))["task_id"]
            self.assertTrue(wait_until(
                lambda: manager.status(task_id)["execution_state"] == EXEC_CRASHED))
        row = self.registry.get_task(task_id)
        self.assertIn("is not supported", row["last_error"])
        self.assertFalse(marker.exists(), "the unsupported runtime must never be started")


# ---------------------------------------------------------------------------
# real-process end to end against a fake RPC binary
# ---------------------------------------------------------------------------


class EndToEndFakeBinaryTests(IsolationTestCase):
    def test_manager_boots_settles_and_applies_isolation_over_real_spawn(self):
        argv_file = self.tmp / "argv.json"
        script = write_script(self.tmp / "fake-pi.py", FAKE_PI_SCRIPT)
        manager = PiManager(registry=self.registry, clock=self.clock,
                            default_thresholds=TEST_THRESHOLDS)  # REAL factory on purpose
        task_id = None
        try:
            with mock.patch.dict(os.environ, {"PI_BIN": str(script),
                                              "PI_FAKE_ARGV_FILE": str(argv_file)}):
                task_id = manager.start_task(prompt="do the thing",
                                             cwd=str(self.cwd_dir))["task_id"]
                self.assertTrue(
                    wait_until(lambda: manager.status(task_id)["execution_state"] == "SETTLED",
                               timeout=20.0),
                    "real-subprocess boot never settled",
                )
            argv = json.loads(argv_file.read_text())
            for flag in MANAGED_CONTEXT_FLAGS:
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--system-prompt") + 1], "")
            policy_path = Path(argv[argv.index("--append-system-prompt") + 1])
            self.assertTrue(policy_path.is_absolute())
            self.assertEqual(policy_path.read_bytes(),
                             (PLUGIN_DIR / "pi-worker.md").read_bytes(),
                             "the executed argv must point at the validated policy snapshot")
            row = self.registry.get_task(task_id)
            self.assertEqual(argv[argv.index("--session") + 1], row["session_file"])
            isolation = [e for e in self.registry.recent_events(task_id, 200)
                         if e["event_type"] == "isolation_applied"]
            self.assertEqual(len(isolation), 1)
            summary = json.loads(isolation[0]["summary"])
            self.assertEqual(summary["id"], MANAGED_ISOLATION_ID)
            self.assertEqual(summary["pi"]["version"], "0.86.0")
            self.assertEqual(row["session_id"], "sess-fake-e2e")
        finally:
            rt = manager._rt(task_id) if task_id else None
            if rt is not None and getattr(rt, "transport", None) is not None:
                rt.transport.terminate()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
