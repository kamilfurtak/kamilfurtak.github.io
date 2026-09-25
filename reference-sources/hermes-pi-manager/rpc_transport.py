"""Pi RPC transport — one Popen, one JSONL reader thread, one command
correlation map, per task.

Real production processes are started with ``pi --mode rpc`` (verified via
``pi --help`` on the installed 0.84.2 binary: ``--mode <mode>`` accepts
``text``, ``json``, or ``rpc``). Tests inject a fake process object via
``popen_factory`` so the whole state machine can be exercised deterministically
without a real Pi/NInfer round trip.

Protocol (verified facts from the installed Pi 0.84.2 RPC source,
``dist/modes/rpc/rpc-mode.js`` / ``rpc-types.d.ts``):
  - stdin/stdout are JSONL.
  - Outgoing commands are parsed by Pi from ``command.type`` (NOT
    ``command.command``), e.g. ``{"type": "get_state", "id": ...}``,
    ``{"type": "prompt", "id": ..., "message": ...}``,
    ``{"type": "steer", "id": ..., "message": ...}``,
    ``{"type": "abort", "id": ...}``,
    ``{"type": "get_entries", "id": ..., "since": ...}``. This module
    assigns a bounded string ``id`` to every outgoing command for
    correlation.
  - Responses (the reverse direction, unaffected by the above) look like
    ``{"type": "response", "command": ..., "success": bool, "id": ...,
    "data": ..., "error": ...}``.
  - Events are any other JSON object read from stdout (``type`` is the
    event name, e.g. ``agent_settled``).
  - Malformed JSONL lines are skipped and recorded, never fatal.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

STDERR_TAIL_MAX_CHARS = 4000
STDERR_TAIL_MAX_LINES = 80

MANAGED_ISOLATION_ID = "managed-context-isolation-v1"

# Per-process agent-dir override used by the isolation probe (pi reads it in
# getAgentDir(); the probe points it at a throwaway directory so personal
# resources and credentials are never read or executed).
ENV_CODING_AGENT_DIR = "PI_CODING_AGENT_DIR"

# Managed-context isolation, verified against the installed pi 0.86.0 loader
# (dist/bundle/chunks/chunk-7YM6BE7Y.js):
#   --no-context-files    AGENTS.md/CLAUDE.md discovery (global, ancestor and
#                         project levels) is skipped: agentsFiles resolves to [].
#   --no-skills           user- and project-level skill discovery is skipped.
#   --no-extensions       extension discovery is skipped; only explicit "-e"
#                         paths load, and settings/package-provided extensions
#                         (packageManager resolution) are skipped as well.
#   --no-prompt-templates prompt-template discovery is skipped.
# Passing --append-system-prompt (which managed runs always do) already
# disables APPEND_SYSTEM.md discovery, and the empty --system-prompt sentinel
# in build_rpc_argv disables SYSTEM.md discovery while keeping pi's default
# base preamble: the loader coalesces "cli value ?? discovered file" and
# resolves an empty string to "no custom prompt".
MANAGED_CONTEXT_FLAGS = (
    "--no-context-files",
    "--no-skills",
    "--no-extensions",
    "--no-prompt-templates",
)


class PopenLike(Protocol):
    """Minimal surface this module needs from a process object.

    ``subprocess.Popen`` satisfies this natively; tests provide fakes.
    """

    pid: int
    stdin: Any
    stdout: Any
    stderr: Any

    def poll(self) -> Optional[int]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: Optional[float] = None) -> int: ...


def find_pi_binary() -> str:
    configured = os.environ.get("PI_BIN")
    candidates: List[str] = []
    if configured:
        candidates.append(configured)
    discovered = shutil.which("pi")
    if discovered:
        candidates.append(discovered)
    candidates.append("/opt/homebrew/bin/pi")
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            return str(Path(candidate).expanduser())
    raise FileNotFoundError("pi binary was not found")


# (pi_bin path + binary identity) -> (ok, version_or_reason). Managed runs
# fail closed when support cannot be proven, so a stale cache entry for a
# replaced binary is prevented by keying on mtime/size.
_SUPPORT_CACHE: Dict[str, tuple] = {}


def pi_isolation_support(pi_bin: str) -> tuple:
    """Whether *pi_bin* reports support for the managed-context isolation flags.

    Returns ``(ok, detail)`` — the reported pi version when supported, else a
    bounded reason. The probe runs ``pi --version`` and ``pi --no-extensions
    --help`` once per binary identity (path + mtime + size) and caches the
    outcome.

    Probe isolation (verified against pi 0.86.0): ``--help`` builds the runtime
    and resource loader BEFORE printing, so extension discovery would EXECUTE
    extension code as a side effect (confirmed live: a marker extension fired
    on plain ``pi --help``). The probe therefore runs with a per-process
    override of ``PI_CODING_AGENT_DIR`` pointing at a temporary EMPTY agent
    directory and with a temporary cwd — personal settings, trust, auth,
    extensions and skills are neither read nor executed — and it passes
    ``--no-extensions`` so even discovery from the (empty) scopes is off.
    ``--version`` never reaches the runtime (verified), but shares the same
    per-process environment for consistency. Neither probe call touches
    production credentials.

    Declaration scope: this probe verifies only that the binary reports a
    plausible version and that its help output LISTS the four isolation flags.
    It does NOT verify flag semantics; in particular the empty
    ``--system-prompt`` suppression contract is exercised live by the
    real-loader suite (``scripts/run-tests.py --suite pi``). A binary whose
    ``--version`` exits non-zero or reports no parseable version, whose
    ``--help`` fails, or whose help output lacks any required flag fails the
    probe with a visible reason — never a silent full-context fallback."""
    try:
        stat = os.stat(pi_bin)
        key = f"{pi_bin}:{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        key = f"{pi_bin}:missing"
    cached = _SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with tempfile.TemporaryDirectory(prefix="pi-isolation-probe-") as probe_home:
            probe_env = dict(os.environ)
            probe_agent_dir = os.path.join(probe_home, "agent")
            os.makedirs(probe_agent_dir, exist_ok=True)
            probe_env[ENV_CODING_AGENT_DIR] = probe_agent_dir
            version_proc = subprocess.run(
                [pi_bin, "--version"], cwd=probe_home, env=probe_env,
                capture_output=True, text=True, timeout=20)
            help_proc = subprocess.run(
                [pi_bin, "--no-extensions", "--help"], cwd=probe_home, env=probe_env,
                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"cannot execute {pi_bin}: {exc}")
        _SUPPORT_CACHE[key] = result
        return result
    problems = []
    version = "unknown"
    version_text = (version_proc.stdout or "").strip()
    if version_proc.returncode != 0:
        problems.append(f"--version exited {version_proc.returncode}")
    if version_text:
        version = version_text.splitlines()[0].strip()
    version_parts = version.split(".")
    if (len(version_parts) < 3
            or not all(part.isdigit() for part in version_parts[:3])):
        problems.append(f"unparseable version output: {version[:60]!r}")
    if help_proc.returncode != 0:
        problems.append(f"--help exited {help_proc.returncode}")
    help_text = (help_proc.stdout or "") + "\n" + (help_proc.stderr or "")
    missing = [flag for flag in MANAGED_CONTEXT_FLAGS if flag not in help_text]
    if missing:
        problems.append("missing flags: " + ", ".join(missing))
    if problems:
        result = (False, f"pi at {pi_bin} is not supported: " + "; ".join(problems))
    else:
        result = (True, version)
    _SUPPORT_CACHE[key] = result
    return result


def default_popen_factory(argv: List[str], cwd: str) -> subprocess.Popen:
    """Real production process factory: pi in RPC mode."""
    return subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def build_rpc_argv(
    pi_bin: str,
    *,
    provider: str,
    model: str,
    thinking: str,
    session_file: Optional[str] = None,
    session_id: Optional[str] = None,
    no_session: bool = False,
    append_system_prompt: Optional[str] = None,
) -> List[str]:
    argv = [pi_bin, "--mode", "rpc", "--provider", provider, "--model", model,
            "--thinking", thinking]
    # Managed-context isolation: no discovered context files, skills,
    # extensions or prompt templates, and no SYSTEM.md/APPEND_SYSTEM.md
    # ingestion (see MANAGED_CONTEXT_FLAGS above).
    argv += list(MANAGED_CONTEXT_FLAGS)
    if session_file:
        argv += ["--session", session_file]
    elif session_id:
        argv += ["--session-id", session_id]
    elif no_session:
        argv += ["--no-session"]
    # Explicit "no SYSTEM.md": the loader only falls back to file discovery
    # when this value is null/undefined, and an empty string resolves to "no
    # custom prompt" — so the default base preamble is kept while a global or
    # trusted-project SYSTEM.md can never inject.
    argv += ["--system-prompt", ""]
    if append_system_prompt is not None:
        # Managed runs pass a manager-owned SNAPSHOT path here. pi resolves
        # this argument as a FILE whenever a file with that name exists
        # (v0.86.0 resolvePromptInput -> existsSync(input)), so a raw text
        # value could be silently reinterpreted against the task cwd; the
        # content-addressed snapshot removes that ambiguity and is
        # independent of later source edits. The recorded hash covers exactly
        # the snapshot's bytes (see core._ensure_policy_snapshot).
        argv += ["--append-system-prompt", append_system_prompt]
    return argv


class RpcTimeoutError(TimeoutError):
    """An RPC command did not receive a response within the timeout."""


class RpcTransportClosed(RuntimeError):
    """The transport's process has already exited."""


class PiRpcTransport:
    """Wraps one Pi RPC process: JSONL send/receive with command correlation.

    ``on_event`` is invoked (from the reader thread) for every parsed line
    that is not itself a correlated command response. ``on_malformed`` is
    invoked for lines that fail to parse as JSON.
    """

    def __init__(
        self,
        process: PopenLike,
        on_event: Callable[[Dict[str, Any]], None],
        on_malformed: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.process = process
        self.pid = getattr(process, "pid", None)
        self._on_event = on_event
        self._on_malformed = on_malformed
        self._write_lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._id_counter = itertools.count(1)
        self._closed = threading.Event()
        self._stderr_tail: List[str] = []
        self._stderr_lock = threading.Lock()

        self._reader_thread = threading.Thread(
            target=self._read_loop, name="pi-rpc-reader", daemon=True
        )
        self._reader_thread.start()
        if getattr(process, "stderr", None) is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, name="pi-rpc-stderr", daemon=True
            )
            self._stderr_thread.start()
        else:
            self._stderr_thread = None

    # -- outgoing --------------------------------------------------------

    def _next_id(self) -> str:
        return f"req-{next(self._id_counter)}-{uuid.uuid4().hex[:8]}"

    def send_command(
        self,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Send a command and block for its correlated response."""
        if self.process.poll() is not None:
            raise RpcTransportClosed(f"process already exited (rc={self.process.poll()})")
        req_id = self._next_id()
        # Real Pi RPC parses outgoing commands from ``type``, not
        # ``command`` (verified against the installed 0.84.2 source) — do
        # not regress this back to ``{"command": ...}``.
        payload = {"type": command, "id": req_id}
        if params:
            payload.update(params)
        event = threading.Event()
        box: Dict[str, Any] = {}
        with self._pending_lock:
            self._pending[req_id] = {"event": event, "box": box}
        line = json.dumps(payload, ensure_ascii=False)
        try:
            with self._write_lock:
                self.process.stdin.write(line + "\n")
                self.process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise RpcTransportClosed(f"failed to write command {command!r}: {exc}") from exc

        if not event.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise RpcTimeoutError(f"command {command!r} timed out after {timeout}s")
        with self._pending_lock:
            self._pending.pop(req_id, None)
        response = box.get("response", {})
        if not response.get("success", False):
            raise RuntimeError(
                f"command {command!r} failed: {response.get('error')!r}"
            )
        return response.get("data") or {}

    def send_prompt(self, text: str) -> str:
        # Real Pi RPC expects the prompt text under "message", not "prompt".
        self.send_command("prompt", {"message": text}, timeout=5.0)
        return "accepted"

    def send_steer(self, text: str) -> str:
        self.send_command("steer", {"message": text}, timeout=5.0)
        return "accepted"

    def send_abort(self, timeout: float = 5.0) -> bool:
        try:
            self.send_command("abort", {}, timeout=timeout)
            return True
        except (RpcTimeoutError, RpcTransportClosed, RuntimeError):
            return False

    def get_state(self, timeout: float = 5.0) -> Dict[str, Any]:
        return self.send_command("get_state", {}, timeout=timeout)

    def get_entries(self, since: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]:
        params = {"since": since} if since else {}
        return self.send_command("get_entries", params, timeout=timeout)

    # -- lifecycle ---------------------------------------------------------

    def terminate(self) -> None:
        try:
            self.process.terminate()
        except Exception:
            pass

    def kill(self) -> None:
        try:
            self.process.kill()
        except Exception:
            pass

    def poll(self) -> Optional[int]:
        return self.process.poll()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)[-STDERR_TAIL_MAX_CHARS:]

    # -- reader threads ------------------------------------------------

    def _drain_stderr(self) -> None:
        try:
            for line in self.process.stderr:
                with self._stderr_lock:
                    self._stderr_tail.append(line.rstrip("\n"))
                    if len(self._stderr_tail) > STDERR_TAIL_MAX_LINES:
                        self._stderr_tail = self._stderr_tail[-STDERR_TAIL_MAX_LINES:]
        except Exception:
            pass

    def _read_loop(self) -> None:
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    if self._on_malformed:
                        try:
                            self._on_malformed(line)
                        except Exception:
                            pass
                    continue
                if not isinstance(obj, dict):
                    if self._on_malformed:
                        try:
                            self._on_malformed(line)
                        except Exception:
                            pass
                    continue
                req_id = obj.get("id")
                if obj.get("type") == "response" and req_id is not None:
                    with self._pending_lock:
                        entry = self._pending.get(req_id)
                    if entry is not None:
                        entry["box"]["response"] = obj
                        entry["event"].set()
                        continue
                    # Unmatched response (late/duplicate) -> treat as event.
                try:
                    self._on_event(obj)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._closed.set()
            # Wake up any commands still waiting — the process end (EOF)
            # means no more responses are coming.
            with self._pending_lock:
                pending = list(self._pending.values())
            for entry in pending:
                entry["event"].set()
