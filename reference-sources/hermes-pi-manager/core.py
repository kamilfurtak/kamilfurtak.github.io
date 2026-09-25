"""PiManager core — task lifecycle, state machine, watchdog, recovery.

Stdlib only. No network, no new dependencies. This module is deliberately
importable stand-alone (no Hermes runtime dependency) so it can be unit
tested with fake RPC processes and exercised from both the Hermes plugin
tools (``tools.py``) and the compatibility CLI (``scripts/pi-execute.py``).

State machine
-------------
execution_state:
    STARTING -> RUNNING -> TOOL_RUNNING/WAITING/STALLED/UNRESPONSIVE -> SETTLED
                                                                      -> CRASHED
                                                                      -> ABORTED

verification_state (independent axis, only meaningful once execution_state
reaches SETTLED):
    NOT_RUN -> PENDING -> PASS | FAIL

The externally-visible "final state" combines both axes (see
``derived_state``): SETTLED+PASS -> DONE, SETTLED+FAIL -> FAILED_VERIFICATION.
A SETTLED task with no verifier stays SETTLED/NOT_RUN — it is NEVER reported
as DONE. ``agent_end`` alone (with or without ``willRetry``) never changes
execution_state to SETTLED; only the literal ``agent_settled`` event does.

Monitoring policy (progress-based, 2026-09-01)
---------------------------------------------
There is NO hard wall-clock deadline as a primary control: a long task with
real progress must not be killed. The optional ``emergency_cap_seconds`` safety
backstop is DISABLED by default (None) and must be set explicitly to take
effect. The watchdog runs on its own ~30 s cadence (independent of the
user-facing notification rate limit) and drives a stale timer off the
persisted ``last_progress_at`` + current-tool (``active_tool``) fields:
any meaningful RPC event (message/streaming/tool start/end/...) refreshes
progress; no progress ~450 s outside a tool, or ~1200 s with an active tool,
requests a Pi RPC abort and opens a 120 s grace -- ``agent_settled`` during
the grace preserves the true result; only an agent that ignores the abort
is finalized STALLED. User progress notices stay separately rate-limited
(first after 90 s, then >=180 s apart, real progress only); terminal
settled/failed/stalled/verifier notices are immediate and ride the durable
notification outbox (see ``outbox.py``), delivered by a worker that calls
the plugin-local host adapter directly (``host_adapter.py`` imports and
directly calls the native ``tools.send_message_tool.send_message_tool``)
-- no LLM/agent turn, no private queue, and no ToolRegistry entry:
core keeps ``send_message`` host-only, so ``send_message`` is deliberately
never registered.

Terminal continuation wake (separate, durable path)
----------------------------------------------------
A task dispatched from a gateway/Telegram session (a valid
``origin.session_key``) resumes the ORIGINAL orchestrator session exactly
once, and only from terminal final states AFTER verification has resolved:
settled with no verifier, verifier PASS/FAIL/UNRUNNABLE, CRASHED, STALLED,
and recovery failure. A manual abort NEVER wakes. The state machine
(NULL -> pending -> dispatching -> accepted, bounded retry back to
pending, 'exhausted' when the budget is spent, and 'uncertain' for a
dispatch that survived a process death and is therefore NEVER retried)
lives on the task row — one logical wake per task, no second outbox table
(``registry_db`` terminal-wake methods). Delivery is the plugin-owned
``TerminalWakeWorker`` (``wake_worker.py``): it holds the fresh
PluginContext as worker integration state only (never in the PiManager
core or on a task) and calls
``ctx.inject_message(message, session_key=origin.session_key)`` — the
ONLY ``inject_message`` path in this plugin. Progress stays passive
(never injected), and the Telegram notification outbox is unchanged
(host adapter -> native send_message_tool -> gateway, zero agent turns).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import os
import signal
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Any, Callable, Dict, List, Optional

try:  # pragma: no cover - normal path: loaded as a real package by Hermes
    from .registry_db import Registry, bound  # type: ignore
    from .executors import validate_spec, validate_profiles
    from . import lsp_check, lsp_feedback, execution_owner  # type: ignore
    from .activity import load_snapshot, activity_summary
    from .outbox import NotificationOutbox, _safe_ref  # type: ignore
    from .rpc_transport import (  # type: ignore
        MANAGED_CONTEXT_FLAGS,
        MANAGED_ISOLATION_ID,
        PiRpcTransport,
        RpcTimeoutError,
        RpcTransportClosed,
        build_rpc_argv,
        default_popen_factory,
        find_pi_binary,
        pi_isolation_support,
    )
except ImportError:  # pragma: no cover - standalone/test import (no package)
    from registry_db import Registry, bound
    from executors import validate_spec, validate_profiles
    import lsp_check, lsp_feedback, execution_owner  # type: ignore
    from activity import load_snapshot, activity_summary
    from outbox import NotificationOutbox, _safe_ref
    from rpc_transport import (
        MANAGED_CONTEXT_FLAGS,
        MANAGED_ISOLATION_ID,
        PiRpcTransport,
        RpcTimeoutError,
        RpcTransportClosed,
        build_rpc_argv,
        default_popen_factory,
        find_pi_binary,
        pi_isolation_support,
    )

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

EXEC_STARTING = "STARTING"
EXEC_RUNNING = "RUNNING"
EXEC_TOOL_RUNNING = "TOOL_RUNNING"
EXEC_WAITING = "WAITING"
EXEC_STALLED = "STALLED"
EXEC_UNRESPONSIVE = "UNRESPONSIVE"
EXEC_SETTLED = "SETTLED"
EXEC_CRASHED = "CRASHED"
EXEC_ABORTED = "ABORTED"

EXEC_FINAL_STATES = {EXEC_SETTLED, EXEC_CRASHED, EXEC_ABORTED}
EXEC_RESUMABLE_STATES = {
    EXEC_STARTING, EXEC_RUNNING, EXEC_TOOL_RUNNING, EXEC_WAITING,
    EXEC_STALLED, EXEC_UNRESPONSIVE,
}

VERIFY_NOT_RUN = "NOT_RUN"
VERIFY_PENDING = "PENDING"
VERIFY_PASS = "PASS"
VERIFY_FAIL = "FAIL"
# The verifier never ran: the command itself could not be executed (127 = not
# found, 126 = found but not executable), or it timed out before producing a
# verdict. This is NOT the same as FAIL. FAIL is a verdict — the gate ran and
# said no. UNRUNNABLE is the absence of one, and the cause can lie on either
# side: a gate naming the wrong path, or a task that was meant to produce the
# file and stopped short. Observed 2026-08-28: three tasks gated on
# `bash -n ops/deploy-cms-strapi.sh` each returned 127, and it read as a
# broken gate right up until the third attempt created the script and passed.
# So the state says "no verdict", and deliberately does not apportion blame.
VERIFY_UNRUNNABLE = "UNRUNNABLE"

DERIVED_DONE = "DONE"
DERIVED_FAILED_VERIFICATION = "FAILED_VERIFICATION"
# Distinct from both DONE and FAILED_VERIFICATION: the work may be fine, but
# nothing checked it because the gate could not run. Folding this into
# SETTLED would make it indistinguishable from "no verifier was configured".
DERIVED_GATE_UNRUNNABLE = "GATE_UNRUNNABLE"

# This prompt is the Pi Manager's REQUIRED leaf-worker system prompt: every
# managed run gets it (the packaged file below) unless the caller supplies an
# explicit ``system_prompt_file`` override. A missing policy file is a hard
# error BEFORE any process is spawned — a worker must never run without its
# rules ("fail closed", see ``_resolve_system_prompt_file``).
# The leaf-worker policy ships WITH the plugin: a policy that does not match
# the manager's expectations is worse than none, and a fresh install has no
# reason to already carry a file under ~/.pi. Versioning them together means
# the worker contract can never silently drift from the code that relies on
# it. There is deliberately no ~/.pi fallback: two sources for one policy is
# the drift this packaging exists to remove.
PACKAGED_SYSTEM_PROMPT_FILE = Path(__file__).with_name("pi-worker.md")
DEFAULT_SYSTEM_PROMPT_FILE = PACKAGED_SYSTEM_PROMPT_FILE

# Leaf-worker policies are small text files; the read is bounded so a special
# file that ever slips past the fd type check can never stream forever.
MAX_POLICY_BYTES = 256 * 1024


class PolicyFileError(RuntimeError):
    """A policy file that must never be consumed as worker policy text.

    ``kind`` distinguishes the cases the snapshot contract depends on:
    ``missing`` (a real absence — the ONLY case that authorises creating a
    new snapshot), ``unreadable``, ``not_regular`` and ``too_large``.
    """

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind

# Events that never mean forward progress alone (handled specially) vs the
# generic "any event refreshes last_progress_at" rule.
SETTLING_EVENT = "agent_settled"
END_EVENT = "agent_end"
START_EVENT = "agent_start"
MESSAGE_START_EVENT = "message_start"
MESSAGE_UPDATE_EVENT = "message_update"
COMPACTION_START_EVENT = "compaction_start"
COMPACTION_END_EVENT = "compaction_end"
TOOL_START_EVENTS = {"tool_execution_start"}
TOOL_UPDATE_EVENTS = {"tool_execution_update"}
TOOL_END_EVENTS = {"tool_execution_end"}
# Events that mean the agent is actively producing output: while any of
# these arrives, the task's status should show is_streaming=1. Kept in sync
# with the real Pi RPC event names (verified in the installed source).
STREAMING_EVENT_TYPES = {START_EVENT, MESSAGE_UPDATE_EVENT} | TOOL_START_EVENTS | TOOL_UPDATE_EVENTS
# Bounded integer ceiling for the event-derived message_count increments;
# get_state snapshots (the authoritative source) may still overwrite it.
MAX_MESSAGE_COUNT = 1_000_000

# Every `except` in this module that reports a swallowed error used `logger`,
# but nothing ever defined it: the handler itself raised NameError, so a
# failing notifier propagated out of _notify_done instead of being logged and
# dropped. test_a_raising_notifier_cannot_break_a_task passed anyway, because
# it only asserts the state persisted BEFORE the notifier ran.
logger = logging.getLogger(__name__)

# Bounds for PiManager.digest. Chosen so a digest of the largest session on
# disk (991 KB) still lands around 2 KB: the point of the tool is that reading
# it must always be cheaper than reading the transcript it replaces.
DIGEST_PROMPT_CHARS = 400
DIGEST_FINAL_CHARS = 1200


def _first_text(parts: List[Any], limit: int) -> str:
    """Bounded concatenation of the ``text`` parts of one message.

    Skips ``thinking`` parts: they are the delegate's scratch reasoning, they
    are the bulkiest thing in a transcript, and they are not what a supervisor
    asked for when it asked what happened.
    """
    chunks = [
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return bound(" ".join(c for c in chunks if c).strip(), limit) or ""


# ---------------------------------------------------------------------------
# Notification message formatting
#
# These strings ARE the chat message: they are delivered to Kamil in
# Telegram, in Polish. Every part is bounded (the outbox additionally caps
# the whole body). Policy:
#   - concise and useful: the task id, active tool, elapsed time, the real
#     verdict (or an explicit "nothing was checked"), and error facts stay;
#   - no raw orchestrator meta ("still working: RUNNING, 55 messages"), no
#     internal instructions (call pi_status/pi_digest), no async-delegation
#     boilerplate, no "Settlement is not acceptance" line;
#   - no full internal paths (cwd etc.) — pi_status is where those live.
# The formatters live here (not in tools.py) so the exact chat text is
# unit-testable without a Hermes runtime.
# ---------------------------------------------------------------------------


_VERDICT_PHRASES = {
    VERIFY_PASS: "Weryfikacja: PASS.",
    VERIFY_FAIL: "Weryfikacja: FAIL — wyniki wymagają przeglądu.",
    VERIFY_UNRUNNABLE: ("Weryfikacja: UNRUNNABLE — nie wydano werdyktu "
                        "(to nie jest FAIL)."),
}
_NO_VERIFIER_PHRASE = ("Weryfikacja: nie uruchomiono (brak weryfikatora) — "
                       "wynik nie został sprawdzony.")


def _runtime_phrase(row: Dict[str, Any]) -> str:
    started = row.get("started_at")
    settled = row.get("settled_at")
    if started and settled and settled >= started:
        return f"{settled - started:.0f} s"
    return ""


def format_completion_message(task_id: str, row: Dict[str, Any]) -> str:
    """The completion notice (terminal kinds 'settled' and 'verifier'):
    states zakończenie and the real verdict when a gate ran, or explicitly
    says nothing checked the output when it did not."""
    verification = row.get("verification_state") or VERIFY_NOT_RUN
    parts = [
        f"Pi zakończył zadanie `{task_id}`.",
        _VERDICT_PHRASES.get(verification, _NO_VERIFIER_PHRASE),
    ]
    # message_count includes prompts and replies, not completed work. The
    # Desktop activity card counts tool calls separately; notices must not
    # turn conversation length into an invented step count.
    rt = _runtime_phrase(row)
    if rt:
        parts.append(f"Czas: {rt}.")
    if row.get("last_error"):
        parts.append(f"Ostatni błąd: {bound(str(row.get('last_error')), 300)}.")
    return " ".join(parts)


def format_failed_message(task_id: str, row: Dict[str, Any]) -> str:
    detail = row.get("last_error")
    exit_code = row.get("exit_code")
    head = (f"Zadanie `{task_id}` zakończyło się z błędem: "
            f"stan {row.get('execution_state')}")
    if exit_code is not None:
        head += f", kod wyjścia {exit_code}"
    head += "."
    if detail:
        head += f" {bound(str(detail), 300)}"
    return f"{head} Wyniki wymagają przeglądu."


def format_stalled_message(task_id: str, row: Dict[str, Any]) -> str:
    active_tool = row.get("active_tool")
    last = row.get("last_progress_at")
    if active_tool:
        cause = f"przy aktywnym narzędziu {bound(str(active_tool), 120)}"
    elif last:
        cause = "bez żadnego zdarzenia w oknie oczekiwania"
    else:
        cause = ""
    return (
        f"Zadanie `{task_id}` jest zawieszone (STALLED): brak postępu do przodu"
        + (f" {cause}" if cause else "")
        + ". Najczęstsza przyczyna: zawieszony proces potomny."
    )


def format_progress_message(task_id: str, row: Dict[str, Any]) -> str:
    """The progress notice: what the task is doing. The
    tool/activity clause is omitted when no activity data is available."""
    text = f"Pi pracuje nad zadaniem `{task_id}`."
    active_tool = row.get("active_tool")
    if active_tool:
        text += f" Aktywne narzędzie: {bound(str(active_tool), 120)}."
    return text


def format_notification_message(kind: str, task_id: str, row: Dict[str, Any]) -> str:
    """Dispatch a notification kind to its bounded message text."""
    if kind == "progress":
        return format_progress_message(task_id, row)
    if kind == "failed":
        return format_failed_message(task_id, row)
    if kind == "stalled":
        return format_stalled_message(task_id, row)
    # settled (ungated) and verifier (gated) both announce completion;
    # the completion text already carries the verdict when one exists.
    return format_completion_message(task_id, row)


# ---------------------------------------------------------------------------
# Terminal continuation wake message
#
# Delivered by the plugin-owned TerminalWakeWorker (wake_worker.py) through
# the matching native host adapter, into the task's OWN orchestrator session —
# the one path in this plugin that may do that. The message is deliberately
# small: task id, both state axes, the continuation id, and the instruction
# to analyze and continue. No transcript, no diff, no Telegram-style body:
# pi_digest is where the work itself is summarized.
# ---------------------------------------------------------------------------

# Identity of THIS Hermes process, minted once per interpreter.
#
# The registry is shared by every process that loads the plugin (gateway,
# each desktop/ACP backend, every interactive CLI), and each of them runs
# its own TerminalWakeWorker against it. A gateway-routed wake carries an
# origin.session_key and can be served by whichever process holds the live
# gateway. A LOCAL wake (interactive CLI: no session_key, delivery goes
# through PluginContext._cli_ref) can only ever be served by the exact
# process that dispatched it — any other worker would call inject_message
# with no CLI reference and no session key, burning the retry budget on a
# delivery it structurally cannot perform, and possibly starving the one
# process that could. Stamping the origin with this value lets the worker
# tell "mine" from "someone else's" without reaching into Hermes internals.
#
# A random id (not the pid) on purpose: pids are reused after a restart,
# and a recycled pid must never let a fresh process adopt a dead one's wake.
HOST_RUNTIME_ID = uuid.uuid4().hex


def continuation_id_for(task_id: str) -> str:
    """The task's single continuation id. Derived, not stored, so the wake
    message and the audit events always agree."""
    return f"wake:{task_id}"


def _compact_verifier_verdict(summary: Dict[str, Any]) -> Optional[str]:
    """Squeeze a verifier summary into one line for the continuation wake.

    The full stdout/stderr tails stay in the event log; the wake only needs
    enough for the orchestrator to decide whether to look further. On a pass
    that is the exit code alone — the point is that a green gate should cost
    no follow-up call at all.
    """
    if not summary:
        return None
    rc = summary.get("returncode")
    if rc == 0:
        return "gate passed (exit 0)"
    diagnosis = summary.get("diagnosis")
    err = summary.get("error")
    tail = (summary.get("stderr_tail") or summary.get("stdout_tail") or "").strip()
    first = tail.splitlines()[-1].strip() if tail else ""
    parts = []
    if rc is not None:
        parts.append(f"gate failed (exit {rc})")
    elif err:
        parts.append(f"gate error: {err}")
    else:
        parts.append("gate did not reach a verdict")
    if diagnosis:
        parts.append(str(diagnosis))
    elif first:
        parts.append(first)
    return " — ".join(parts)


def format_terminal_wake_message(task_id: str, row: Dict[str, Any]) -> str:
    """The ONE terminal continuation wake for a task (see module docstring)."""
    execution_state = row.get("execution_state") or "?"
    verification_state = row.get("verification_state") or VERIFY_NOT_RUN
    gate = row.get("verifier_summary")
    lsp = row.get("lsp_summary")

    # The wake used to carry two enum values and the instruction "go and
    # look", which cost a pi_digest call and a second turn on EVERY finished
    # task — including the ones where nothing needed thinking about. Carrying
    # the verdict makes the clean case free and gives the failing case its
    # first concrete fact without a round-trip.
    lines = [
        f"[pi-manager continuation {continuation_id_for(task_id)}] "
        f"Task `{task_id}` reached its terminal state "
        f"(execution_state={execution_state}, verification_state={verification_state})."
    ]
    if gate:
        lines.append(f"Gate: {gate}")
    if lsp:
        lines.append(f"Semantic check: {lsp}")
    elif verification_state != VERIFY_NOT_RUN or gate:
        # Absence of an LSP verdict is information too: say so rather than
        # letting silence read as "clean".
        lines.append("Semantic check: not available for this task.")

    # SETTLED + NOT_RUN is NOT done. _notify_done exists precisely to avoid
    # announcing "finished" without saying whether the gate passed, and an
    # ungated task never reached a verdict about its work. Only an actually
    # passing gate, with no semantic errors, earns the no-follow-up path.
    clean = (execution_state == EXEC_SETTLED
             and verification_state == VERIFY_PASS
             and not (lsp or "").startswith("LSP found"))
    if clean:
        lines.append(
            "This is the complete outcome — continue the parent workflow "
            "autonomously without re-reading the task. Call pi_digest only "
            "if you actually need the account of what it DID."
        )
    else:
        lines.append(
            f"Analyze the result, then continue the parent workflow "
            f"autonomously (pi_digest for `{task_id}` has the ~2 KB account "
            f"of what it did; pi_status has the raw row)."
        )
    lines.append(
        "This wake fires once per task, after the verifier resolved. Do not "
        "duplicate work that already succeeded; retry or launch a replacement "
        "Pi task only when the terminal state, verification result, or "
        "available evidence justifies it."
    )
    return " ".join(lines)


# Only these fields mean "the agent moved". Hashing the WHOLE get_state
# payload made liveness self-referential: the watchdog's own 2 s
# diagnostic_snapshot rows land in recent_events, their timestamps differ
# every tick, the hash changes, and _apply_state_snapshot refreshes
# last_progress_at — so a dead agent looked alive. Observed 2026-08-27 on
# task pi-16d3ff7c3f14: progress_age 225 s against event_age 848 s, 167
# snapshots, STALLED masked until the hard timeout fired 14 minutes in.
PROGRESS_FIELDS = ("messageCount", "pendingMessageCount", "isStreaming",
                   "isCompacting", "activeTool", "activeRequestId")


def compute_state_hash(state: Dict[str, Any]) -> str:
    """Deterministic SHA-256 over the progress-bearing fields of a
    ``get_state`` payload only (see PROGRESS_FIELDS). Canonical JSON
    (sorted keys, no whitespace ambiguity) so semantically identical
    states always hash identically regardless of key order. Fields outside
    PROGRESS_FIELDS — clocks, counters, echoed diagnostics — must never
    influence liveness."""
    bounded = {k: state.get(k) for k in PROGRESS_FIELDS if k in state}
    canonical = json.dumps(bounded, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derived_state(execution_state: str, verification_state: str) -> str:
    if execution_state == EXEC_SETTLED:
        if verification_state == VERIFY_PASS:
            return DERIVED_DONE
        if verification_state == VERIFY_FAIL:
            return DERIVED_FAILED_VERIFICATION
        if verification_state == VERIFY_UNRUNNABLE:
            return DERIVED_GATE_UNRUNNABLE
        return EXEC_SETTLED
    return execution_state


@dataclass
class Thresholds:
    """Injectable timing thresholds — production defaults per the spec."""

    heartbeat_seconds: float = 20.0
    rpc_timeout_seconds: float = 5.0
    waiting_seconds: float = 10.0
    soft_stall_seconds: float = 450.0
    # Separate, larger quiet threshold for TOOL_RUNNING. Raised from 300 s:
    # `nx run-many -t build,test,lint --skipNxCache` over this monorepo was
    # measured at 5 m 19 s (319 s) on 2026-08-27, i.e. a legitimate gate
    # tripped the old threshold by 19 s. Tool liveness is now also proven
    # directly (see _tool_process_active) rather than inferred from silence.
    tool_stall_seconds: float = 1200.0
    # After the quiet threshold trips, the watchdog requests an RPC abort
    # and gives the agent THIS long to prove settlement. Settlement during
    # the grace preserves the true result (SETTLED + real verdict); only
    # an agent that ignores the abort is finalized STALLED. An UNCHANGED
    # get_state snapshot or any real event during the grace resets it.
    stall_grace_seconds: float = 120.0
    # No wall-clock deadline on a healthy task (Kamil 2026-08-27, confirmed
    # again 2026-09-01: long-running Pi with real progress must not be
    # killed). A task ends at agent_settled; failure is STALLED / CRASHED /
    # UNRESPONSIVE, each reached through evidence. `emergency_cap_seconds`
    # is an OPTIONAL operator-set safety backstop only and is DELIBERATELY
    # DISABLED BY DEFAULT (None): nothing in this plugin kills a task on
    # elapsed time unless the operator explicitly installs a ceiling.
    emergency_cap_seconds: Optional[float] = None  # disabled by default
    terminate_grace_seconds: float = 5.0
    # Watchdog cadence: one evaluation pass per ~30 s. The watchdog is the
    # ONLY stall control and runs on its own clock, fully independent of the
    # user-facing notification rate limit (first progress notice after
    # 90 s, then at most one per 180 s).
    watchdog_interval_seconds: float = 30.0
    # Proof-of-work window for an active tool: if the child process tree
    # burned CPU within this many seconds, the tool is working and silence
    # is not a stall.
    tool_activity_window_seconds: float = 60.0
    # How long an UNCHANGED diagnostic_snapshot is suppressed for. The
    # watchdog ticks every ~30 s; without this a quiet task writes one
    # identical row per tick (see _diagnostic_snapshot). A changed snapshot
    # ignores this entirely and is written on the tick it changes.
    snapshot_repeat_seconds: float = 60.0
    # Progress-notification pacing (durable outbox, plugin-owned):
    # the FIRST progress notice arrives only after this much real elapsed
    # time (a task that settles in a minute is noise nobody asked for),
    # and after that at most one notice per interval. Both are hard caps:
    # hard caps: a notice is additionally gated on REAL progress since the
    # last one (and since the boot baseline before the first notice —
    # state hash / message count / last RPC event type must have moved), so
    # a silent task never produces a notice on schedule.
    progress_first_seconds: float = 90.0
    progress_interval_seconds: float = 180.0


@dataclass
class VerifierSpec:
    argv: List[str]
    timeout_seconds: float = 120.0
    cwd: Optional[str] = None


@dataclass
class _TaskRuntime:
    """In-process (non-persisted) runtime state for one active task."""

    task_id: str
    transport: Optional[PiRpcTransport] = None
    thresholds: Thresholds = field(default_factory=Thresholds)
    verifier: Optional[VerifierSpec] = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    stall_grace_started_at: Optional[float] = None
    pre_unresponsive_state: Optional[str] = None
    watchdog_thread: Optional[threading.Thread] = None
    stop_watchdog: threading.Event = field(default_factory=threading.Event)
    lsp_feedback: Optional[Any] = None
    abort_lock: threading.RLock = field(default_factory=threading.RLock)
    boot_pending: bool = False
    cleanup_pending: bool = False
    now_fn: Callable[[], float] = time.time
    last_snapshot_sig: Optional[str] = None
    last_snapshot_at: Optional[float] = None
    last_progress_notified_at: Optional[float] = None
    last_progress_notified_sig: Optional[tuple] = None


def _safe_steer(rt: Any, text: str) -> None:
    """Steer text into a live task's transport; no-op when it is not up yet."""
    transport = getattr(rt, "transport", None)
    if transport is None:
        return
    transport.send_steer(text)


class PiManager:
    """Reusable core: task lifecycle, registry persistence, watchdog, recovery.

    ``popen_factory`` and ``clock`` are injectable for deterministic tests.
    ``clock`` defaults to ``time.time``; tests may pass a fake monotonically
    advancing clock so watchdog thresholds are exercised without real sleeps.
    """

    def __init__(
        self,
        registry: Registry,
        popen_factory: Callable[[List[str], str], Any] = default_popen_factory,
        clock: Callable[[], float] = time.time,
        default_thresholds: Optional[Thresholds] = None,
        provider: str = "ninfer",
        model: str = "qwen3.8-27b",
        thinking: str = "medium",
        notifier: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        progress_notifier: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        outbox: Optional[NotificationOutbox] = None,
        activity_recorder: Any = None,
        executor_profiles: Optional[Dict[str, Any]] = None,
        default_executor: Optional[str] = None,
    ) -> None:
        # Optional completion callback. The manager stays free of any Hermes
        # import: tools.py supplies something that can reach the conversation,
        # tests supply a list append. A notifier that raises or hangs must
        # never affect a task, so every call is wrapped and best-effort.
        self._notifier = notifier
        self._progress_notifier = progress_notifier
        self._activity_recorder = activity_recorder
        # The durable, plugin-owned notification outbox (see outbox.py).
        # When set, progress and terminal notices are enqueued as rows; the
        # delivery worker in tools.py drains them by calling the
        # plugin-local host adapter directly, which imports and directly
        # calls the native send_message_tool (no registry dispatch, no
        # LLM/agent turn). The legacy notifier callbacks above stay
        # available for existing callers and are independent of the outbox.
        self.outbox = outbox
        self.registry = registry
        self._popen_factory = popen_factory
        self._clock = clock
        self.default_thresholds = default_thresholds or Thresholds()
        self.provider = provider
        self.model = model
        self.thinking = thinking
        self._executor_profiles = (validate_profiles(executor_profiles, default_executor)
                                   if executor_profiles is not None else None)
        if executor_profiles is None and default_executor is not None:
            raise ValueError("default executor requires profiles")
        self._default_executor = default_executor
        self._runtimes: Dict[str, _TaskRuntime] = {}
        self._runtimes_lock = threading.RLock()
        self._execution_locks = {}
        # Non-reentrant on purpose: it is a "one prune at a time" gate, not a
        # critical section.
        self._prune_gate = threading.Semaphore(1)
        # Teardown flag: once shutdown() has run, _start_watchdog refuses,
        # so a still-booting task thread can never resurrect a watchdog
        # after the plugin has begun tearing down.
        self._shutdown_flag = threading.Event()

    # -- helpers ---------------------------------------------------------

    def _now(self) -> float:
        return self._clock()

    def _claim_execution(self, task_id):
        with self._runtimes_lock:
            if task_id in self._execution_locks:
                return False
            fd = execution_owner.acquire(self.registry.path.parent / 'execution-locks', task_id)
            if fd is None:
                return False
            self._execution_locks[task_id] = fd
            return True

    def _release_execution(self, task_id):
        with self._runtimes_lock:
            rt = self._runtimes.get(task_id)
            if rt and (rt.boot_pending or rt.cleanup_pending):
                return
            fd = self._execution_locks.pop(task_id, None)
            if fd is not None:
                execution_owner.release(fd)

    def _rt(self, task_id: str) -> Optional[_TaskRuntime]:
        with self._runtimes_lock:
            return self._runtimes.get(task_id)

    def _record_event(
        self, task_id: str, source: str, event_type: str,
        before: Optional[str] = None, after: Optional[str] = None,
        summary: Optional[Any] = None,
    ) -> None:
        self.registry.append_event(task_id, source, event_type, before, after, summary)

    def _set_execution_state(self, task_id: str, new_state: str, *,
                             recovery: bool = False, **extra: Any) -> None:
        row = self.registry.get_task(task_id)
        before = row.get("execution_state") if row else None
        now = self._now()
        fields = dict(extra)
        fields["execution_state"] = new_state
        if before != new_state:
            fields["last_state_changed_at"] = now
        if recovery:
            self.registry.update_task(task_id, **fields)
        else:
            fields.pop("execution_state")
            changed_at = fields.pop("last_state_changed_at", None)
            if not self.registry.transition_task_state(task_id, new_state, changed_at, **fields):
                return
        if before != new_state:
            self._record_event(task_id, "manager", "state_transition", before, new_state)
        if new_state in EXEC_FINAL_STATES and before != new_state and self._activity_recorder is not None:
            # Terminal state: close the live view. The marker is only a flag;
            # the recorder publishes it together with the final data on its
            # next flush, so viewers stop by explicit confirmation.
            try:
                self._activity_recorder.finalize(task_id)
            except Exception:
                logger.warning("Pi live view could not be finalized; task continues", exc_info=True)

    def _legacy_executor(self):
        return validate_spec({'provider': self.provider, 'model': self.model,
                              'thinking': self.thinking})

    def _select_executor(self, requested):
        if self._executor_profiles is None:
            if requested is not None:
                raise ValueError('executor profiles are not configured')
            return None, self._legacy_executor()
        name = self._default_executor if requested is None else requested
        if not isinstance(name, str) or name not in self._executor_profiles:
            raise ValueError('unknown executor profile')
        return name, dict(self._executor_profiles[name])

    def _stored_executor(self, row):
        raw = (row or {}).get('executor_spec')
        if raw is None:
            # Compatibility for pre-profile tasks only in legacy mode. Once
            # profiles are enabled we cannot infer the historical executor.
            if self._executor_profiles is not None:
                raise ValueError('missing recorded executor')
            return self._legacy_executor()
        try:
            return validate_spec(json.loads(raw))
        except (ValueError, TypeError):
            raise ValueError('invalid recorded executor') from None

    def _executor_identity_error(self, row, state):
        # Registry reads intentionally return None after shutdown. Never admit
        # a prompt without the durable task snapshot or crash the boot thread.
        if not row:
            return 'executor_task_unavailable'
        # Legacy mode preserves the pre-profile RPC compatibility contract.
        if row.get('executor_profile') is None:
            return None
        spec = self._stored_executor(row)
        model = state.get('model')
        if not isinstance(model, dict):
            return 'executor_identity_missing'
        if (model.get('provider') != spec['provider'] or model.get('id') != spec['model']
                or state.get('thinkingLevel') != spec['thinking']):
            return 'executor_identity_mismatch'
        return None

    @staticmethod
    def _executor_metadata(row):
        if not row.get('executor_spec'):
            return None  # Unknown historical selection, not today's default.
        try:
            return {'profile': row.get('executor_profile'),
                    **validate_spec(json.loads(row['executor_spec']))}
        except (ValueError, TypeError):
            return {'error': 'invalid recorded executor'}

    # -- starting a new task ---------------------------------------------

    def start_task(
        self,
        prompt: str,
        cwd: str,
        task_id: Optional[str] = None,
        thresholds: Optional[Thresholds] = None,
        verifier: Optional[VerifierSpec] = None,
        system_prompt_file: Optional[str] = None,
        origin: Optional[Dict[str, Any]] = None,
        executor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start one task. Returns metadata immediately; does not block on
        settlement. The actual RPC start (get_state, then prompt) happens on
        a background thread so callers get task metadata right away, per the
        ``pi_task`` tool contract ("start ... return task metadata without
        waiting for completion")."""
        profile, spec = self._select_executor(executor)
        task_id = task_id or f"pi-{uuid.uuid4().hex[:12]}"
        thresholds = thresholds or self.default_thresholds
        cwd = str(Path(cwd).expanduser())

        if not self._claim_execution(task_id):
            raise RuntimeError('task already has a live execution owner')
        try:
            return self._start_task_owned(task_id, prompt, cwd, thresholds, verifier, system_prompt_file, origin, profile, spec)
        except BaseException:
            self._release_execution(task_id)
            raise

    def _start_task_owned(self, task_id, prompt, cwd, thresholds, verifier, system_prompt_file, origin, profile, spec):
        now = self._now()
        # Allocate a unique, durable session file BEFORE the boot thread
        # starts: <registry-parent>/sessions/<uuid>.jsonl. The exact path is
        # persisted (session_file AND expected_session_file) before boot, so
        # a managed task never relies on Pi's implicit default session
        # storage and recovery can reopen this exact file later.
        session_dir = self.registry.path.parent / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = str(session_dir / f"{uuid.uuid4().hex}.jsonl")
        try:
            Path(session_file).touch()
        except OSError:
            pass
        self.registry.create_task(
            task_id=task_id,
            executor_profile=profile, executor_spec=json.dumps(spec),
            cwd=cwd,
            execution_state=EXEC_STARTING,
            verification_state=VERIFY_NOT_RUN,
            verifier_spec=json.dumps(
                {"argv": verifier.argv, "timeout_seconds": verifier.timeout_seconds}
            ) if verifier else None,
            session_file=session_file, expected_session_file=session_file,
            created_at=now, updated_at=now, started_at=now,
            last_event_at=now, last_state_at=now, last_progress_at=now,
            last_state_changed_at=now,
            # Routing origin snapshot, taken by the caller on the dispatching
            # turn (contextvars do not survive into the settle thread). Only
            # serializable values are accepted: the notifier reads this row
            # back after settlement, possibly after a manager restart.
            origin=bound(json.dumps(origin or {}, ensure_ascii=False, default=str), 2000)
            if origin else None,
            # Terminal continuation wake eligibility. Two routes reach a
            # live orchestrator, and a task qualifies on either one:
            #   - origin.session_key: a gateway/Telegram session, resumed by
            #     PluginContext.inject_message's gateway path;
            #   - origin.host_runtime_id: the dispatching process itself,
            #     resumed by inject_message's _cli_ref path (session_key is
            #     None there — the CLI branch is checked first and never
            #     consults it). Only that same process may serve the wake;
            #     the worker enforces it.
            # A task with no origin snapshot at all (no session_key, no
            # runtime id) stays wake-disabled; existing rows default to 0
            # via the schema.
            continuation_enabled=1 if (
                _safe_ref((origin or {}).get("session_key"))
                or _safe_ref((origin or {}).get("host_runtime_id"))
            ) else 0,
        )
        self._record_event(task_id, "manager", "task_created", None, EXEC_STARTING,
                            summary={"cwd": cwd, "session_file": session_file,
                                     "verifier": bool(verifier),
                                     "origin_platform": (origin or {}).get("platform", ""),
                                     "has_origin_chat_id": bool((origin or {}).get("chat_id"))})
        if verifier is None:
            # Not an error — a read-only audit legitimately has nothing to gate.
            # But a task with no verifier can only ever end at NOT_RUN, so
            # "settled" will carry no evidence that the work is any good. Record
            # the omission so it is auditable after the fact instead of being
            # invisible, and so "how many tasks ran ungated" is one query away.
            self._record_event(task_id, "manager", "verifier_absent", None, None,
                               summary={"note": "no verifier_argv: task can only "
                                                "reach verification_state=NOT_RUN"})

        rt = _TaskRuntime(task_id=task_id, thresholds=thresholds, verifier=verifier,
                           now_fn=self._now, boot_pending=True)
        with self._runtimes_lock:
            self._runtimes[task_id] = rt
        # Live LSP feedback: tail the session file and steer fresh diagnostics
        # back into the task while it still runs. Best-effort by construction.
        try:
            pump = lsp_feedback.LspFeedbackPump(
                task_id=task_id, cwd=cwd, session_file=session_file,
                steer=lambda text: _safe_steer(rt, text),
                record=lambda summary: self._record_event(
                    task_id, "lsp", "feedback", None, None, summary=summary),
            )
            pump.start()
            rt.lsp_feedback = pump
        except Exception:  # noqa: BLE001 - feedback must never break dispatch
            logger.warning("pi-manager: lsp feedback pump unavailable", exc_info=True)

        thread = threading.Thread(
            target=self._boot_and_run,
            args=(task_id, prompt, cwd, system_prompt_file, session_file),
            name=f"pi-task-{task_id}", daemon=True,
        )
        thread.start()
        return {"task_id": task_id, "cwd": cwd, "execution_state": EXEC_STARTING,
                "session_file": session_file, "executor": {"profile": profile, **spec}}

    @staticmethod
    def _read_regular_policy_bytes(resolved: Path, source: str) -> bytes:
        """Read *resolved* from the object actually opened.

        The type check runs against the OPEN fd (``fstat`` + ``S_ISREG``), not
        against an earlier look at the path: a FIFO, device or socket — passed
        directly or swapped in after any precheck — is rejected before a byte
        is read, so a writer-less FIFO can never block the boot path.
        ``O_NONBLOCK`` bounds the open itself for FIFOs, a symlink to a
        regular file stays allowed (the open follows it and the fd is a
        regular file), and the read is capped at ``MAX_POLICY_BYTES``.
        """
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(resolved, flags)
        except FileNotFoundError:
            raise PolicyFileError(
                f"pi-manager: leaf-worker policy file is missing ({source}: {resolved}) — "
                "refusing to start a task without its policy", kind="missing") from None
        except OSError as exc:
            raise PolicyFileError(
                f"pi-manager: leaf-worker policy file is unreadable ({source}: {resolved}: {exc}) — "
                "refusing to start a task without its policy", kind="unreadable") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                if stat.S_ISDIR(st.st_mode):
                    kind = "directory"
                elif stat.S_ISFIFO(st.st_mode):
                    kind = "FIFO"
                elif stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
                    kind = "device"
                elif stat.S_ISSOCK(st.st_mode):
                    kind = "socket"
                else:
                    kind = "special file"
                raise PolicyFileError(
                    f"pi-manager: leaf-worker policy path is not a regular file ({kind}: {source}: {resolved}) — "
                    "refusing to start a task without its policy", kind="not_regular")
            chunks: List[bytes] = []
            remaining = MAX_POLICY_BYTES + 1
            while remaining > 0:
                data = os.read(fd, min(65536, remaining))
                if not data:
                    break
                chunks.append(data)
                remaining -= len(data)
            raw = b"".join(chunks)
        except OSError as exc:
            raise PolicyFileError(
                f"pi-manager: leaf-worker policy file is unreadable ({source}: {resolved}: {exc}) — "
                "refusing to start a task without its policy", kind="unreadable") from None
        finally:
            os.close(fd)
        if len(raw) > MAX_POLICY_BYTES:
            raise PolicyFileError(
                f"pi-manager: leaf-worker policy file is too large (> {MAX_POLICY_BYTES} bytes: {source}: {resolved}) — "
                "refusing to start a task without its policy", kind="too_large")
        return raw

    def _load_system_prompt(self, override: Optional[str]) -> tuple:
        """Load and validate the leaf-worker policy for a managed run.

        An explicit override always wins over the packaged default, but both
        must resolve to a READABLE REGULAR FILE with valid UTF-8 and non-empty
        (not whitespace-only) content: missing, unreadable, mis-encoded, empty
        or directory paths are hard errors BEFORE any process is spawned (fail
        closed — a worker must never run without its rules).

        Path base: an explicit RELATIVE override is resolved against the
        MANAGER's current working directory (documented contract), and the
        returned source path is always absolute — validation, hashing and the
        recorded evidence refer to one unambiguous file even when the task
        runs in a different cwd.

        Returns ``(source_path, snapshot_path, sha256_prefix)``. The bytes are
        read ONCE from the open fd, and the child receives a manager-owned,
        content-addressed SNAPSHOT path (see ``_ensure_policy_snapshot``) —
        not the original path and not the raw text. pi resolves
        ``--append-system-prompt`` as a file whenever a file with that name
        exists (verified v0.86.0: ``existsSync(input)``), so raw text can be
        reinterpreted against the task cwd; the snapshot removes that
        ambiguity and is also independent of later replacement or removal of
        the source. The recorded hash covers exactly the snapshot's bytes.
        """
        if override:
            candidate = Path(override).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            source = "system_prompt_file override"
        else:
            candidate = DEFAULT_SYSTEM_PROMPT_FILE
            source = "packaged default"
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved.is_dir():
            raise RuntimeError(
                f"pi-manager: leaf-worker policy path is a directory ({source}: {resolved}) — "
                "refusing to start a task without its policy")
        raw = self._read_regular_policy_bytes(resolved, source)
        if not raw:
            raise RuntimeError(
                f"pi-manager: leaf-worker policy file is empty ({source}: {resolved}) — "
                "refusing to start a task without its policy")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError(
                f"pi-manager: leaf-worker policy file is not valid UTF-8 ({source}: {resolved}) — "
                "refusing to start a task without its policy") from None
        if not text.strip():
            raise RuntimeError(
                f"pi-manager: leaf-worker policy has no non-whitespace content ({source}: {resolved}) — "
                "refusing to start a task without its policy")
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = self._ensure_policy_snapshot(raw, digest, source, resolved)
        return str(resolved), snapshot, digest[:16]

    def _inspect_policy_snapshot(self, target: Path) -> tuple:
        """Classify the snapshot address WITHOUT trusting a path precheck.

        Returns ``(status, error)`` with status in ``{"missing", "ok",
        "invalid"}``. The read runs through the same fd guard as the source
        policy (regular file, ``O_NONBLOCK``, capped size), so a FIFO under
        the snapshot address cannot stall the boot path, and an unreadable,
        oversize or mis-hashed file is reported as ``invalid`` — never as
        "absent", because only a real absence may authorise creation.
        """
        try:
            raw = self._read_regular_policy_bytes(target, "policy snapshot")
        except PolicyFileError as exc:
            if exc.kind == "missing":
                return "missing", None
            return "invalid", str(exc)
        if hashlib.sha256(raw).hexdigest() != target.stem:
            return "invalid", (
                f"pi-manager: policy snapshot content does not match its "
                f"address ({target})")
        return "ok", None

    def _ensure_policy_snapshot(self, raw: bytes, digest: str, source: str,
                                resolved: Path) -> str:
        """Publish (once) the validated policy bytes to a private snapshot file.

        The snapshot lives under ``<state>/policy-snapshots/<sha256>.md`` in
        the manager's own state directory and is addressed by content, so the
        path the child receives always contains exactly the validated bytes,
        independent of the source file and of the task cwd.

        Contract for EXISTING addresses:
        - missing            -> create (only a real absence authorises creation)
        - valid (re-hashed)  -> reuse as-is, never rewritten
        - invalid (mismatched, special, too large, unreadable) -> controlled
          error BEFORE any spawn; the file is left untouched for inspection
          (no auto-repair, no silent deletion).

        Publication never replaces an existing address: the temp file is
        claimed with ``O_EXCL`` and linked into place with ``os.link`` (which
        raises instead of overwriting), so two managers racing on a first
        creation converge on one inode — the loser verifies the winner's file
        and uses it only if it is valid. Temp files are cleaned up on every
        path including errors. Same-user tampering with the state dir during
        the spawn window stays outside this contract (same trust domain as
        the registry).
        """
        root = self.registry.path.parent / "policy-snapshots"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / f"{digest}.md"

        status, error = self._inspect_policy_snapshot(target)
        if status == "ok":
            return str(target)
        if status == "invalid":
            raise PolicyFileError(
                f"pi-manager: policy snapshot exists but is not usable "
                f"({source}: {resolved} -> {target}: {error}) — refusing to "
                "start a task; the existing file is left untouched",
                kind="snapshot_invalid")

        tmp = root / f".{digest}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, target)
            except FileExistsError:
                pass  # a concurrent creator claimed the address first
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

        status, error = self._inspect_policy_snapshot(target)
        if status != "ok":
            raise PolicyFileError(
                f"pi-manager: policy snapshot verification failed "
                f"({source}: {resolved} -> {target}: {error}) — refusing to "
                "start a task without its verified policy",
                kind="snapshot_invalid")
        return str(target)

    def _spawn_process(self, task_id: str, cwd: str, session_file: Optional[str] = None,
                        no_session: bool = False,
                        system_prompt_file: Optional[str] = None,
                        executor_spec: Optional[Dict[str, str]] = None) -> Any:
        real = self._popen_factory is default_popen_factory
        pi_bin = find_pi_binary() if real else "pi"
        spec = validate_spec(executor_spec) if executor_spec is not None else self._legacy_executor()
        # Policy first: a bad policy must fail before even the helper probe
        # process runs, and the validated text + hash are bound together here.
        policy_source, policy_snapshot, policy_sha = self._load_system_prompt(system_prompt_file)
        pi_version = ""
        if real:
            # Compatibility gate: managed runs REQUIRE a pi that supports the
            # isolation flags. Fail closed with a clear error before any work
            # is sent — never silently fall back to full-context discovery.
            ok, detail = pi_isolation_support(pi_bin)
            if not ok:
                raise RuntimeError(
                    "pi-manager: refusing to start a managed task — " + detail +
                    " (upgrade pi or set PI_BIN to a supported binary)"
                )
            pi_version = detail
        argv = build_rpc_argv(
            pi_bin, **spec,
            session_file=session_file, no_session=no_session,
            append_system_prompt=policy_snapshot,
        )
        # Evidence linkage: the isolation configuration of this spawn ATTEMPT,
        # recorded with the existing event mechanism (no schema change). It is
        # recorded before Popen and is configuration evidence only — not proof
        # that the process started or that work was sent.
        self._record_event(task_id, "manager", "isolation_applied", None, None, summary={
            "id": MANAGED_ISOLATION_ID,
            "flags": list(MANAGED_CONTEXT_FLAGS),
            "system_prompt": "suppressed",
            "policy": {"source": policy_source, "snapshot": policy_snapshot,
                       "sha256": policy_sha, "delivery": "snapshot-path"},
            "pi": {"path": pi_bin, "version": pi_version or "test-double"},
        })
        return self._popen_factory(argv, cwd)

    def _boot_and_run(self, task_id: str, prompt: str, cwd: str,
                       system_prompt_file: Optional[str],
                       session_file: Optional[str]) -> None:
        rt = self._rt(task_id)
        if rt is None:
            return
        try:
            self._boot_process(task_id, prompt, cwd, system_prompt_file, session_file)
        finally:
            with rt.abort_lock:
                rt.boot_pending = False
                row = self.registry.get_task(task_id) or {}
                if (not row or self._shutdown_flag.is_set() or
                        (row.get('execution_state') in EXEC_FINAL_STATES
                         and row.get('verification_state') != VERIFY_PENDING)):
                    self._release_execution(task_id)

    def _cancel_start_if_requested(self, task_id: str, rt: _TaskRuntime) -> bool:
        # The caller owns abort_lock. It is deliberately NOT held across spawn.
        row = self.registry.get_task(task_id) or {}
        if row.get('abort_requested'):
            self._complete_abort(task_id, rt, 'startup_cancelled')
            return True
        return row.get('execution_state') in EXEC_FINAL_STATES

    def _boot_process(self, task_id: str, prompt: str, cwd: str,
                      system_prompt_file: Optional[str],
                      session_file: Optional[str]) -> None:
        rt = self._rt(task_id)
        if rt is None:
            return
        with rt.abort_lock:
            if self._cancel_start_if_requested(task_id, rt):
                return
        try:
            # Managed tasks ALWAYS get an explicit "--session <path>" for
            # the pre-allocated file (persisted before this thread started);
            # Pi's implicit default session storage and "--no-session" are
            # both off-limits for managed tasks.
            process = self._spawn_process(task_id, cwd, session_file=session_file,
                                           system_prompt_file=system_prompt_file,
                                           executor_spec=self._stored_executor(self.registry.get_task(task_id)))
        except Exception as exc:
            with rt.abort_lock:
                if self._cancel_start_if_requested(task_id, rt):
                    return
            self._set_execution_state(task_id, EXEC_CRASHED,
                                       last_error=bound(f"spawn failed: {exc}"))
            self._notify_failed(task_id)
            self._request_terminal_wake(task_id)
            return

        transport = PiRpcTransport(
            process,
            on_event=lambda ev: self._on_event(task_id, ev, source="rpc"),
            on_malformed=lambda line: self._on_malformed(task_id, line),
        )
        with rt.abort_lock:
            rt.transport = transport
            self.registry.update_task(task_id, pid=getattr(process, "pid", None))
            if self._cancel_start_if_requested(task_id, rt):
                return

        try:
            state = transport.get_state(timeout=rt.thresholds.rpc_timeout_seconds)
        except (RpcTimeoutError, RpcTransportClosed, RuntimeError) as exc:
            self._handle_early_failure(task_id, transport, f"initial get_state failed: {exc}")
            return

        # Identity gate BEFORE anything from the response is persisted:
        # the pre-allocated session file is the ground truth, and the
        # initial get_state must prove the process actually owns it.
        session_id = state.get("sessionId")
        reported_file = state.get("sessionFile")
        identity_error = self._boot_identity_error(session_id, reported_file, session_file)
        if identity_error is not None:
            # Reject: record a bounded startup/session identity
            # diagnostic, terminate the process, and park the task in an
            # explicit non-success state. The prompt is NEVER sent to a
            # process whose session identity is unverified, and the
            # persisted (expected) identity is left exactly as allocated.
            self._record_event(
                task_id, "manager", "session_identity_rejected", None, None,
                summary={"reason": bound(identity_error)},
            )
            self._handle_early_failure(
                task_id, transport, f"startup session identity rejected: {identity_error}",
            )
            return
        executor_row = self.registry.get_task(task_id)
        executor_error = self._executor_identity_error(executor_row, state)
        if executor_error:
            self._handle_early_failure(task_id, transport, executor_error)
            return
        self._record_event(task_id, 'manager', 'executor_identity_checked', None, None,
                           summary={'checked': executor_row.get('executor_profile') is not None})
        self.registry.update_task(
            task_id, session_id=session_id, expected_session_id=session_id,
        )
        # Initial boot snapshot: there is no prior hash, so this always
        # counts as the first "change" and seeds last_state_hash/
        # last_state_changed_at for subsequent heartbeats to compare against.
        self._apply_state_snapshot(task_id, state)
        # Baseline for the progress gate: a silent task must not emit its
        # first progress notice at the 90 s mark (see _seed_progress_baseline).
        self._seed_progress_baseline(task_id, rt)
        self._record_event(task_id, "manager", "identity_established", EXEC_STARTING,
                            EXEC_STARTING, summary={"session_id": session_id})

        # Prompt admission and abort share one per-task linearization point.
        # RPC is bounded; no registry/global lock is held across this I/O.
        with rt.abort_lock:
            if self._cancel_start_if_requested(task_id, rt):
                return
            try:
                transport.send_prompt(prompt)
            except (RpcTimeoutError, RpcTransportClosed, RuntimeError) as exc:
                self._handle_early_failure(task_id, transport, f"prompt not accepted: {exc}")
                return
            self._set_execution_state(task_id, EXEC_RUNNING, last_progress_at=self._now())
            self._start_watchdog(task_id)

    @staticmethod
    def _boot_identity_error(session_id: Any, reported_file: Any,
                              allocated_session_file: Optional[str]) -> Optional[str]:
        """Return a bounded rejection reason when the initial get_state
        response cannot prove the process owns the pre-allocated session,
        or None when the identity is verified.

        A managed task is always spawned with an explicit
        ``--session <allocated>`` path, so a conforming response must
        report: a non-empty sessionId, a sessionFile, and a sessionFile
        whose canonical path equals the allocated one. Anything else is
        a startup failure, never a diagnostic-only mismatch."""
        if session_id is None or not str(session_id).strip():
            return "missing_session_id: initial get_state reported no non-empty sessionId"
        if not reported_file:
            return "missing_session_file: initial get_state reported no sessionFile"
        if _canonical_path(str(reported_file)) != _canonical_path(allocated_session_file):
            return (f"session_file_mismatch: initial get_state reported "
                    f"{bound(str(reported_file))} but the task was allocated "
                    f"{bound(allocated_session_file)}")
        return None

    def _handle_early_failure(self, task_id: str, transport: PiRpcTransport, message: str) -> None:
        rt = self._rt(task_id)
        if rt is not None:
            with rt.abort_lock:
                if self._cancel_start_if_requested(task_id, rt):
                    return
        transport.terminate()
        self._set_execution_state(
            task_id, EXEC_CRASHED,
            last_error=bound(message), stderr_tail=bound(transport.stderr_tail()),
        )
        self._notify_failed(task_id)
        # Startup CRASHED is a terminal state the orchestrator must act on:
        # a spawn/identity failure means the delegated work never ran.
        self._request_terminal_wake(task_id)

    # -- notification enqueueing (durable outbox) --------------------------

    def _enqueue_notification(self, task_id: str, kind: str) -> Optional[str]:
        """Best-effort enqueue of one notification row. Returns the
        notification_id, or None when there is no outbox, when the message
        could not be built, or when a row with the same stable id already
        exists (local dedupe). A failure here NEVER disturbs a task — the
        outbox is a reporting channel, not part of the task's outcome."""
        if self.outbox is None:
            return None
        try:
            row = self.registry.get_task(task_id) or {}
            message = format_notification_message(kind, task_id, row)
            if kind == 'progress':
                data = load_snapshot(self.registry.path.parent / 'activity', task_id)
                summary = activity_summary(data)
                if summary:
                    message += '\n' + summary
                if 'tools_completed' in data:
                    message += f"\nUkończone wywołania narzędzi: {data['tools_completed']}."
            return self.outbox.enqueue(task_id, kind, message)
        except Exception as exc:
            logger.debug("outbox enqueue failed for %s (%s): %s", task_id, kind, exc)
            return None

    def _notify_failed(self, task_id: str) -> None:
        self._stop_feedback(task_id)
        self._enqueue_notification(task_id, "failed")
        self._release_execution(task_id)

    def _notify_stalled(self, task_id: str) -> None:
        self._enqueue_notification(task_id, "stalled")

    def _request_terminal_wake(self, task_id: str) -> Optional[str]:
        """Open the task's single terminal continuation wake (idempotent).

        Called ONLY from terminal transitions whose outcome the orchestrator
        should act on: settlement without a verifier, verifier
        PASS/FAIL/UNRUNNABLE, CRASHED, STALLED, and recovery failure. NEVER
        for a manual abort — an explicitly killed task must not turn its
        session back on. The registry CAS makes the "once per task"
        guarantee durable across restarts. Best effort: a failure here never
        disturbs the task's outcome (the Telegram outbox is independent)."""
        try:
            row = self.registry.get_task(task_id)
            if row is None or row.get("abort_requested"):
                return None
            # Only the FIRST transition is audited: re-observations of an
            # already-handled wake (recovery re-runs, explicit re-announce)
            # return the pre-existing state and record nothing.
            was_unopened = row.get("wake_state") is None
            state = self.registry.begin_terminal_wake(task_id, self._now())
            if was_unopened and state in ("pending", "disabled"):
                self._record_event(
                    task_id, "manager", f"wake_{state}", row.get("execution_state"),
                    None, summary={
                        "execution_state": row.get("execution_state"),
                        "verification_state": row.get("verification_state"),
                        "continuation_id": continuation_id_for(task_id),
                    },
                )
            return state or row.get("wake_state")
        except Exception as exc:
            logger.debug("terminal wake request failed for %s: %s", task_id, exc)
            return None

    def _on_malformed(self, task_id: str, raw_line: str) -> None:
        self._record_event(task_id, "rpc", "malformed_jsonl", None, None,
                            summary={"raw": raw_line[:200]})

    # -- event handling ----------------------------------------------------

    def _on_event(self, task_id: str, event: Dict[str, Any], source: str = "rpc") -> None:
        rt = self._rt(task_id)
        row = self.registry.get_task(task_id)
        if row is None:
            return
        if row.get("abort_requested"):
            return
        etype = event.get("type") or event.get("event") or "unknown"
        if source == "rpc" and self._activity_recorder is not None:
            try:
                self._activity_recorder.observe(task_id, event)
            except Exception:
                logger.warning("Pi live view rejected an event; task continues", exc_info=True)
        now = self._now()
        before = row["execution_state"]
        updates: Dict[str, Any] = {
            "last_event_at": now, "last_event_type": etype, "last_progress_at": now,
        }

        if before in (EXEC_UNRESPONSIVE,):
            # A real event proves liveness even before a heartbeat succeeds.
            before_for_recover = (rt.pre_unresponsive_state if rt and rt.pre_unresponsive_state
                                   else EXEC_RUNNING)
        else:
            before_for_recover = before

        new_state = before_for_recover

        if etype == START_EVENT:
            new_state = EXEC_RUNNING
        elif etype in TOOL_START_EVENTS:
            new_state = EXEC_TOOL_RUNNING
            # active_tool is the persisted "current tool": set on
            # tool_execution_start, cleared on tool_execution_end.
            updates["active_tool"] = event.get("tool") or event.get("toolName")
            updates["active_request_id"] = event.get("toolCallId") or event.get("requestId")
        elif etype in TOOL_UPDATE_EVENTS:
            new_state = EXEC_TOOL_RUNNING
        elif etype in TOOL_END_EVENTS:
            new_state = EXEC_RUNNING
            updates["active_tool"] = None
            updates["active_request_id"] = None
        elif etype == END_EVENT:
            # agent_end NEVER settles the task; honor willRetry by simply
            # continuing to monitor. Not a state change by itself unless we
            # were degraded (STALLED/WAITING/UNRESPONSIVE) — an agent_end
            # proves the process is alive and progressing.
            if before_for_recover in (EXEC_STALLED, EXEC_WAITING, EXEC_UNRESPONSIVE):
                new_state = EXEC_RUNNING
            # willRetry is preserved in the bounded event-log summary
            # (``_bounded_event_summary`` keeps it); it is NOT a tasks-table
            # column — persisting it used to raise in update_task and
            # silently drop the entire agent_end update.
        elif etype == SETTLING_EVENT:
            new_state = EXEC_SETTLED
            updates["settled_at"] = now
            updates["verification_state"] = VERIFY_PENDING if (rt and rt.verifier) else VERIFY_NOT_RUN
        elif etype == "entry_appended":
            # Real Pi RPC shape: {"type": "entry_appended", "entry": {"id":
            # ...}} — the id is nested inside "entry". Top-level entryId/id
            # is kept only as a defensive fallback, never the primary path.
            nested_entry = event.get("entry")
            entry_id = nested_entry.get("id") if isinstance(nested_entry, dict) else None
            if not entry_id:
                entry_id = event.get("entryId") or event.get("id")
            if entry_id:
                updates["last_entry_id"] = str(entry_id)
            if before_for_recover in (EXEC_STALLED, EXEC_WAITING, EXEC_UNRESPONSIVE):
                new_state = EXEC_RUNNING
        else:
            # message_start/update/end, turn_start/end, compaction_*,
            # auto_retry_* — all count as progress; recover degraded states.
            if before_for_recover in (EXEC_STALLED, EXEC_WAITING, EXEC_UNRESPONSIVE):
                new_state = EXEC_RUNNING

        # -- event-derived status metrics (between get_state heartbeats) --
        # get_state remains the authoritative snapshot; these keep the
        # status fields meaningful when a short task settles before the
        # next periodic heartbeat. None of this is an acceptance or
        # verification signal (that axis stays entirely separate), and the
        # event summary logged below stays bounded (no message/token
        # payloads).
        if etype == MESSAGE_START_EVENT:
            current_count = row.get("message_count")
            try:
                current_count = int(current_count)
            except (TypeError, ValueError):
                current_count = 0
            updates["message_count"] = min(current_count + 1, MAX_MESSAGE_COUNT)
            if before not in EXEC_FINAL_STATES:
                updates["is_streaming"] = 1
        elif etype in STREAMING_EVENT_TYPES:
            # A settled/final task must not be made to look live again.
            if before not in EXEC_FINAL_STATES:
                updates["is_streaming"] = 1
        elif etype == COMPACTION_START_EVENT:
            updates["is_compacting"] = 1
        elif etype == COMPACTION_END_EVENT:
            updates["is_compacting"] = 0
        elif etype == END_EVENT:
            # agent_end stops the stream but is still not settlement.
            updates["is_streaming"] = 0
        elif etype == SETTLING_EVENT:
            # Clears both status flags while execution_state=SETTLED is
            # persisted by the block below; the later manager-initiated
            # process disposal is recorded as exit_code only, never as a
            # CRASHED classification.
            updates["is_streaming"] = 0
            updates["is_compacting"] = 0

        # -- state transition (atomic CAS) --------------------------------
        # Written via Registry.transition_task_state, which applies it only
        # if the currently persisted state is not final: the row snapshot at
        # the top of this method can be stale (the watchdog tick may have
        # finalized the task between the read and this write), and a final
        # state must never be resurrected by a stale snapshot. Other fields
        # (metrics, timestamps, flags) still go through the plain update
        # below.
        if before not in EXEC_FINAL_STATES:
            if new_state != before:
                if not self.registry.transition_task_state(task_id, new_state, now):
                    return

        self.registry.update_task(task_id, **updates)
        self._record_event(task_id, source, etype, before,
                            (new_state if before not in EXEC_FINAL_STATES
                             else before),
                            summary=_bounded_event_summary(event))

        if rt is not None:
            with rt.lock:
                rt.stall_grace_started_at = None
                rt.pre_unresponsive_state = None
            # The boot's agent_start is part of the initial steady state,
            # not work: before any progress notice has gone out, re-baseline
            # on it so a SILENT task (agent_start, then nothing) never emits
            # a first progress notice at the 90 s mark. A progressing task's
            # later real events (message_start, tool_*, ...) move the
            # signature and permit the notice.
            if etype == START_EVENT and rt.last_progress_notified_at is None:
                row_now = self.registry.get_task(task_id)
                if row_now is not None and rt.last_progress_notified_sig is not None:
                    self._set_progress_baseline(rt, row_now)

        if etype == SETTLING_EVENT:
            self._on_settled(task_id)

    def _prune_registry(self) -> None:
        """Bound the event log's growth, off the caller's thread.

        Nothing pruned before this existed, and by 2026-08-28 the registry had
        reached 46 MB across 336 353 rows, 94% of them message_update.

        Runs on its own thread because the delete holds the registry lock and
        the caller is the RPC reader thread, so doing it inline stalls the
        OTHER parallel task's event stream. A steady-state prune is ~100 ms,
        but the backlog grows with the gap since the last one — the first prune
        on this registry cleared 225 578 rows. Retention is hygiene: it must
        never make a task wait, and a failure here must never touch a task's
        outcome.
        """
        if not self._prune_gate.acquire(blocking=False):
            return  # a prune is already in flight; a second adds nothing
        threading.Thread(target=self._prune_now, name="pi-prune", daemon=True).start()

    def _prune_now(self) -> None:
        try:
            deleted = self.registry.prune_events()
            if deleted:
                logger.info("pi-manager: pruned %d aged high-volume events", deleted)
        except Exception as exc:
            logger.warning("pi-manager: event prune failed: %s", exc)
        finally:
            self._prune_gate.release()

    def _on_settled(self, task_id: str) -> None:
        # execution_state=SETTLED (plus settled_at, the event log, and the
        # verification axis) is already persisted by the caller (_on_event)
        # BEFORE this runs — the event, never the process exit, is the
        # settlement proof. Only now do we dispose of the long-lived Pi RPC
        # process: installed Pi 0.84.2's runRpcMode() intentionally never
        # exits on its own after a command, so the manager must actively
        # stop it once settlement is semantically established.
        rt = self._rt(task_id)
        self._stop_watchdog(task_id)
        self._dispose_process_after_settlement(task_id, rt)
        self._prune_registry()
        # Both paths run off-thread now: the LSP pass reads files from disk
        # and talks to language servers, and the settle path must not wait
        # on that. An ungated task still gets announced — it is usually a
        # read-only audit, and the whole reason for notifying is that nobody
        # should have to ask.
        threading.Thread(
            target=self._run_verifier, args=(task_id,),
            name=f"pi-verify-{task_id}", daemon=True,
        ).start()

    def _dispose_process_after_settlement(
        self, task_id: str, rt: Optional[_TaskRuntime],
    ) -> None:
        """Terminate (then kill if needed) the per-task Pi RPC process once
        settlement is already persisted. Bounded and defensive: any error
        here is recorded as a diagnostic event, never allowed to corrupt
        execution_state (which stays SETTLED regardless). The watchdog is
        already stopped by the caller, so the process exiting here can
        never be mistaken for an unexpected crash — that detection path
        only runs from ``watchdog_tick``, which will not run again for this
        task. ``pi_resume`` can still reopen the persisted session file
        later; disposing the live process does not touch it."""
        if rt is None or rt.transport is None:
            return
        transport = rt.transport
        thresholds = rt.thresholds
        try:
            if transport.poll() is None:
                transport.terminate()
                exit_code = transport.wait(timeout=thresholds.terminate_grace_seconds)
                if exit_code is None:
                    transport.kill()
                    exit_code = transport.wait(timeout=thresholds.terminate_grace_seconds)
                if exit_code is not None:
                    # Manager-initiated cleanup: record the resulting exit
                    # code for diagnostics, but NEVER flip a SETTLED task
                    # to CRASHED/ABORTED because of this disposal.
                    self.registry.update_task(task_id, exit_code=exit_code)
        except Exception as exc:  # pragma: no cover - defensive only
            self._record_event(task_id, "manager", "dispose_error", None, None,
                                summary=str(exc))

    # -- verification ------------------------------------------------------

    def _notify_done(self, task_id: str) -> None:
        """Announce a finished task, once, after verification has resolved.

        Delegates used to finish in silence: the registry recorded the settle
        and nothing told anyone. On 2026-08-28 a task settled at 06:41:00 and
        was only noticed at 06:47:22, when a human asked. Announcing at
        agent_settled would be too early — it would say "done" without saying
        whether the gate passed, which is the distinction this plugin exists
        to keep.

        With a durable outbox attached, the announcement is enqueued NOW as a
        terminal notification (kind 'verifier' when a gate ran — the message
        carries the real verdict; kind 'settled' when nothing was attached to
        check) and delivered by the worker. The stable per-(task, kind) id
        makes it idempotent across recovery re-runs. The legacy notifier
        callback, when provided, is still invoked in parallel.

        This is also the moment the terminal continuation wake is requested
        (when the task is eligible): it is called from here — never from the
        settlement event itself — so the wake can only ever carry a
        RESOLVED verification state, and a manual abort (which never runs
        _notify_done) can never trigger one.
        """
        row = self.registry.get_task(task_id) or {}
        if row.get("verification_state") in (VERIFY_PASS, VERIFY_FAIL, VERIFY_UNRUNNABLE):
            kind = "verifier"
        else:
            kind = "settled"
        self._enqueue_notification(task_id, kind)
        self._request_terminal_wake(task_id)
        self._release_execution(task_id)
        if self._notifier is None:
            return
        try:
            payload = {
                "task_id": task_id,
                "execution_state": row.get("execution_state"),
                "verification_state": row.get("verification_state"),
                "message_count": row.get("message_count"),
                "runtime_seconds": (
                    round((row.get("settled_at") or 0) - (row.get("started_at") or 0), 1)
                    if row.get("settled_at") else None
                ),
                "cwd": row.get("cwd"),
                "last_error": row.get("last_error"),
            }
            self._notifier(task_id, payload)
        except Exception as exc:  # never let a notifier disturb a task
            logger.debug("notifier failed for %s: %s", task_id, exc)

    def _notify_progress(self, task_id: str, rt: _TaskRuntime) -> None:
        """Bounded, rate-limited progress notice on REAL progress only.

        Gating (all hard): (1) nothing before ``progress_first_seconds`` of
        task age (90 s); (2) at most one notice per
        ``progress_interval_seconds`` (180 s); (3) the progress signature
        (state hash, message count, last real RPC event type) must have
        moved since the last notice — and before the first notice it must
        have moved since the boot baseline (see _seed_progress_baseline),
        so a silent task never notifies on schedule. The
        durable row (progress kind, monotonic id) is what reaches the chat;
        the legacy progress_notifier callback, when provided, is still
        invoked.
        """
        row = self.registry.get_task(task_id)
        if row is None or row["execution_state"] in EXEC_FINAL_STATES:
            return
        now = self._now()
        started_at = row.get("started_at") or now
        thresholds = rt.thresholds
        if now - started_at < thresholds.progress_first_seconds:
            return
        last = rt.last_progress_notified_at
        if last is not None and now - last < thresholds.progress_interval_seconds:
            return  # the interval gate binds subsequent notices only; the
            # first one is bounded by progress_first_seconds alone
        # Real progress only: the state hash, message count, and the last
        # REAL RPC event type must have moved. last_event_type is what makes
        # event activity count as progress even when an unchanged get_state
        # snapshot (the authoritative source) reverts the transient
        # event-derived message count — and a completely silent task
        # produces no events, so it stays equal to the boot baseline. (The
        # boot's own agent_start is re-baselined, see _on_event; the
        # watchdog's WAITING relabel is excluded because it is not an RPC
        # event.)
        signature = (row.get("last_state_hash"), row.get("message_count"),
                     row.get("last_event_type"))
        if (rt.last_progress_notified_sig is not None
                and signature == rt.last_progress_notified_sig):
            return  # no real progress since the last notice
        rt.last_progress_notified_at = now
        rt.last_progress_notified_sig = signature
        self._enqueue_notification(task_id, "progress")
        if self._progress_notifier is None:
            return
        try:
            payload = {
                "task_id": task_id,
                "execution_state": row.get("execution_state"),
                "message_count": row.get("message_count"),
                "is_streaming": row.get("is_streaming"),
            }
            self._progress_notifier(task_id, payload)
        except Exception as exc:
            logger.debug("progress notifier failed for %s: %s", task_id, exc)

    def _seed_progress_baseline(self, task_id: str, rt: "_TaskRuntime") -> None:
        """Pin the progress-notification signature to the task's INITIAL
        signature (the boot/recovery snapshot's state hash + message count +
        last event type).

        Edge case this fixes: before the first evaluation the gate had no
        baseline to compare against (``last_progress_notified_sig`` was
        None), so a completely SILENT task that was still running past the
        90 s mark would emit its first progress notice on schedule. With the
        baseline seeded from the initial snapshot, a silent task emits
        nothing until the signature actually changes — real progress (new
        message count, changed state hash, or real RPC event activity) is
        what permits the first notice, on the normal 90 s first / 180 s
        subsequent cadence.

        The boot's own ``agent_start`` arrives AFTER this boot snapshot; it
        is boot confirmation, not work, so ``_on_event`` re-baselines once
        on it (and only before any progress notice has gone out).
        """
        row = self.registry.get_task(task_id)
        if row is None:
            return
        with rt.lock:
            if rt.last_progress_notified_sig is None:
                self._set_progress_baseline(rt, row)

    def _set_progress_baseline(self, rt: "_TaskRuntime", row: Dict[str, Any]) -> None:
        """Write the progress baseline (caller holds nothing; takes
        rt.lock)."""
        with rt.lock:
            rt.last_progress_notified_sig = (
                row.get("last_state_hash"), row.get("message_count"),
                row.get("last_event_type"))

    def _stop_feedback(self, task_id: str) -> None:
        """Stop the live LSP pump once the task is terminal."""
        rt = self._rt(task_id)
        if rt is not None and rt.lsp_feedback is not None:
            rt.lsp_feedback.stop()

    def _run_verifier(self, task_id: str) -> None:
        """The terminal step: semantic check, then the gate, then the notice.

        Runs for gated AND ungated tasks. The LSP pass goes first so its
        verdict is on the row before anything announces or wakes, and it is
        deliberately independent of the gate: a task with no verifier is
        exactly the case where a type error would otherwise ship unnoticed.
        """
        self._stop_feedback(task_id)
        self._run_lsp_check(task_id)
        rt = self._rt(task_id)
        try:
            if rt is not None and rt.verifier is not None:
                self.run_verifier_now(task_id, rt.verifier)
        finally:
            # Announce even when the verifier itself blew up: a task that
            # finished and could not be checked is exactly what an operator
            # needs told, and silence there would be the worst outcome.
            self._notify_done(task_id)

    def _run_lsp_check(self, task_id: str) -> None:
        """Diagnose what Pi wrote with the host's language servers.

        Pi ships no LSP, so this is the only semantic check the work gets.
        It costs zero agent turns: ``lsp_check`` calls the host service
        in-process, exactly like the outbox calls ``send_message_tool``.

        Best-effort by construction — a NULL ``lsp_summary`` means "no
        verdict", never "clean", and no failure here may affect the gate or
        the announcement.
        """
        try:
            row = self.registry.get_task(task_id)
            if row is None:
                return
            result = lsp_check.run(row.get("cwd"))
            summary = lsp_check.format_summary(result)
            if summary is None:
                return
            self.registry.update_task(task_id, lsp_summary=bound(summary, 600))
            self._record_event(task_id, "verifier", "lsp_check", None, None,
                               summary=result)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pi-manager: LSP check skipped for %s: %s", task_id, exc)

    def run_verifier_now(self, task_id: str, verifier: VerifierSpec) -> str:
        """Run the verifier command (argv, no shell) and derive PASS/FAIL.

        Runs strictly AFTER settlement — callers must not invoke this before
        execution_state == SETTLED. Returns the resulting verification_state.
        """
        import subprocess

        row = self.registry.get_task(task_id)
        if row is None or row.get("execution_state") != EXEC_SETTLED:
            raise RuntimeError("verifier may only run after execution_state == SETTLED")
        self.registry.update_task(task_id, verification_state=VERIFY_PENDING)
        cwd = verifier.cwd or row.get("cwd")
        try:
            result = subprocess.run(
                verifier.argv, cwd=cwd, capture_output=True, text=True,
                timeout=verifier.timeout_seconds, check=False, shell=False,
            )
            summary = {
                "returncode": result.returncode,
                "stdout_tail": bound(result.stdout, 800),
                "stderr_tail": bound(result.stderr, 800),
            }
            if result.returncode == 0:
                outcome = VERIFY_PASS
            elif result.returncode in (126, 127):
                outcome = VERIFY_UNRUNNABLE
                summary["diagnosis"] = (
                    f"the verifier command could not be executed (exit "
                    f"{result.returncode}); no verdict was reached about the "
                    "work. Either the gate names something that does not exist, "
                    "or the task was supposed to create it and did not — check "
                    "which before re-dispatching."
                )
            else:
                outcome = VERIFY_FAIL
        except subprocess.TimeoutExpired as exc:
            # No verdict was reached, so this is not evidence the work is bad.
            outcome = VERIFY_UNRUNNABLE
            summary = {"error": bound(str(exc)),
                       "diagnosis": "the verifier timed out before returning a verdict"}
        except Exception as exc:
            outcome = VERIFY_FAIL
            summary = {"error": bound(str(exc))}
        self.registry.update_task(
            task_id, verification_state=outcome,
            verifier_summary=bound(_compact_verifier_verdict(summary), 400),
        )
        self._record_event(task_id, "verifier", "verifier_run", VERIFY_PENDING, outcome, summary)
        if outcome != VERIFY_PASS:
            self._warn_on_repeat(task_id, verifier, row, summary.get("returncode"))
        return outcome

    def _warn_on_repeat(self, task_id: str, verifier: VerifierSpec,
                        row: Dict[str, Any], returncode: Optional[int]) -> None:
        """Say so when the same gate has now failed the same way more than once.

        Three tasks in a row on 2026-08-28 were gated on
        `bash -n ops/deploy-cms-strapi.sh` — a file present nowhere in the
        repository — and each 127 was recorded as an isolated failure. Nothing
        in the registry connected them, so the third was dispatched with the
        identical gate against the identical worktree while the first two
        results were already on disk. Repetition is the signal that the gate,
        not the work, is what needs looking at.
        """
        try:
            argv = list(verifier.argv)
            cwd = row.get("cwd")
            prior = [
                t for t in self.registry.list_tasks()
                if t.get("task_id") != task_id
                and t.get("cwd") == cwd
                and t.get("verification_state") in (VERIFY_FAIL, VERIFY_UNRUNNABLE)
                and _load_verifier_spec(t.get("verifier_spec")) is not None
                and list(_load_verifier_spec(t.get("verifier_spec")).argv) == argv
            ]
            if not prior:
                return
            self._record_event(
                task_id, "verifier", "verifier_repeat_failure", None, None,
                summary={
                    "argv": argv,
                    "cwd": cwd,
                    "returncode": returncode,
                    "previous_task_ids": [t["task_id"] for t in prior][-5:],
                    "attempt": len(prior) + 1,
                    "note": "the same gate has now failed in this cwd more than "
                            "once; re-dispatching without changing it will most "
                            "likely produce the same result",
                },
            )
        except Exception as exc:
            logger.debug("repeat-failure check skipped for %s: %s", task_id, exc)

    # -- watchdog ------------------------------------------------------

    def _start_watchdog(self, task_id: str) -> None:
        rt = self._rt(task_id)
        if rt is None or self._shutdown_flag.is_set():
            return
        if rt.watchdog_thread is not None:
            return
        rt.stop_watchdog.clear()
        thread = threading.Thread(
            target=self._watchdog_loop, args=(task_id,),
            name=f"pi-watchdog-{task_id}", daemon=True,
        )
        rt.watchdog_thread = thread
        thread.start()

    def _stop_watchdog(self, task_id: str) -> None:
        rt = self._rt(task_id)
        if rt is None:
            return
        rt.stop_watchdog.set()

    def _watchdog_loop(self, task_id: str) -> None:
        rt = self._rt(task_id)
        if rt is None:
            return
        while not rt.stop_watchdog.is_set():
            try:
                self.watchdog_tick(task_id)
                self._notify_progress(task_id, rt)
            except Exception:
                pass
            row = self.registry.get_task(task_id)
            if row is None or row.get("execution_state") in EXEC_FINAL_STATES:
                return
            rt.stop_watchdog.wait(rt.thresholds.watchdog_interval_seconds)

    def watchdog_tick(self, task_id: str) -> None:
        """One synchronous watchdog evaluation step — public so tests can
        drive it deterministically with a fake clock instead of real sleeps."""
        rt = self._rt(task_id)
        row = self.registry.get_task(task_id)
        if rt is None or row is None:
            return
        if row["execution_state"] in EXEC_FINAL_STATES:
            return

        now = self._now()
        thresholds = rt.thresholds

        # Infrastructure backstop only — NOT the normal end of a task. A
        # healthy task ends at agent_settled; an unhealthy one ends at
        # STALLED / CRASHED / UNRESPONSIVE, each reached through evidence.
        # Time since start is deliberately not a failure condition.
        cap = thresholds.emergency_cap_seconds
        if cap is not None:
            started_at = row.get("started_at") or now
            if now - started_at >= cap:
                self._emergency_cap_abort(task_id)
                return

        # Process liveness (CRASHED detection).
        if rt.transport is not None and rt.transport.poll() is not None:
            exit_code = rt.transport.poll()
            self._set_execution_state(
                task_id, EXEC_CRASHED, exit_code=exit_code,
                last_error=bound(f"process exited before settlement (rc={exit_code})"),
                stderr_tail=bound(rt.transport.stderr_tail()),
            )
            self._notify_failed(task_id)
            self._request_terminal_wake(task_id)
            return

        current = row["execution_state"]

        # --- Periodic get_state heartbeat.
        # get_state is a liveness/consistency check, not the event source:
        # it is due purely on elapsed time since the last get_state
        # (heartbeat_seconds), EVEN BEFORE the soft-stall threshold.
        last_state_at = row.get("last_state_at") or now
        unchanged_heartbeat_state = None
        if now - last_state_at >= thresholds.heartbeat_seconds:
            heartbeat_ok, state_payload, state_changed = self._heartbeat(task_id, rt)
            if not heartbeat_ok:
                # A heartbeat timeout marks UNRESPONSIVE even if the last
                # event/progress was recent — never wait for soft_stall.
                self._mark_unresponsive(task_id, rt, current)
                return
            if state_changed:
                self._apply_changed_heartbeat(task_id, rt, current)
                return
            # Unchanged heartbeat: last_state_at only was refreshed (by
            # ``_apply_state_snapshot``); fall through to stall logic.
            unchanged_heartbeat_state = state_payload

        idle = now - (row.get("last_progress_at") or now)
        quiet_threshold = (
            thresholds.tool_stall_seconds if current == EXEC_TOOL_RUNNING
            else thresholds.soft_stall_seconds
        )

        if idle < thresholds.waiting_seconds:
            return  # actively progressing; nothing to do

        if idle < quiet_threshold:
            if current in (EXEC_RUNNING, EXEC_WAITING):
                self._set_execution_state(task_id, EXEC_WAITING)
            # TOOL_RUNNING stays TOOL_RUNNING while under its own, larger
            # quiet threshold — a long tool must not be relabeled WAITING.
            return

        # idle >= quiet_threshold: heartbeat (if not already issued above)
        # -> diagnostic snapshot -> grace -> STALLED
        if unchanged_heartbeat_state is not None:
            # Already issued this tick and unchanged: reuse the payload
            # instead of sending a second get_state in the same tick.
            state_payload, state_changed = unchanged_heartbeat_state, False
        else:
            heartbeat_ok, state_payload, state_changed = self._heartbeat(task_id, rt)
            if not heartbeat_ok:
                self._mark_unresponsive(task_id, rt, current)
                return
            if state_changed:
                self._apply_changed_heartbeat(task_id, rt, current)
                return

        # Heartbeat succeeded but the state hash is UNCHANGED: process is
        # alive and RPC-responsive but truly quiet.
        #
        # Before calling that a stall, ask the OS. A running tool can be
        # silent for many minutes and still be doing exactly what it was
        # asked to do; RPC silence alone is not evidence of a stall. If the
        # process tree burned CPU since the last sample, renew the progress
        # lease and record why. Absence of proof falls through to the normal
        # diagnostic path — it is never treated as proof of idleness.
        if current == EXEC_TOOL_RUNNING:
            activity = self._tool_process_active(
                rt, thresholds.tool_activity_window_seconds)
            if activity is not None:
                self.registry.update_task(task_id, last_progress_at=now)
                self._record_event(task_id, "watchdog", "tool_activity_observed",
                                    None, None, summary=activity)
                with rt.lock:
                    rt.stall_grace_started_at = None
                return

        # Take a bounded diagnostic snapshot, then apply the grace period.
        self._diagnostic_snapshot(task_id, rt, state_payload)
        with rt.lock:
            grace_starting = rt.stall_grace_started_at is None
            if grace_starting:
                rt.stall_grace_started_at = now
            grace_started = rt.stall_grace_started_at
        # Progress-based stall policy (2026-09-01): on the FIRST tick past
        # the quiet threshold, request an RPC abort and give the agent a
        # grace period. If it settles during the grace, _on_event already
        # persisted SETTLED and the watchdog loop will exit — the true
        # result is preserved. Only an agent that ignores the abort is
        # finalized STALLED below. Any real event or a CHANGED get_state
        # snapshot clears stall_grace_started_at (see _on_event and
        # _apply_changed_heartbeat), which resets the stale timer.
        if grace_starting:
            self._send_stall_abort(task_id, rt)
        if now - grace_started >= thresholds.stall_grace_seconds:
            if current != EXEC_STALLED:
                # Record WHAT is hanging at the moment we call it a stall.
                # Without this the registry says only "stalled", and finding a
                # hung child means walking the process table by hand.
                kids = self._live_descendants(rt)
                self._record_event(
                    task_id, "watchdog", "stall_diagnosis", None, None,
                    summary={"active_tool": row.get("active_tool"),
                             "idle_seconds": round(now - (row.get("last_progress_at") or now), 1),
                             "live_descendants": kids,
                             "note": "a descendant still alive after "
                                     "tool_execution_end is an orphaned tool "
                                     "process and the usual cause"},
                )
            self._set_execution_state(task_id, EXEC_STALLED)
            # First entry only (guarded by current != EXEC_STALLED); the
            # stable notification id makes re-entry after a recovery heal a
            # no-op anyway.
            self._notify_stalled(task_id)
            self._request_terminal_wake(task_id)
        elif current not in (EXEC_STALLED,):
            self._set_execution_state(task_id, EXEC_WAITING)

    def _send_stall_abort(self, task_id: str, rt: _TaskRuntime) -> None:
        """Request an RPC abort once, when the stall grace starts.

        Best effort: a failed abort request (transport already gone) is
        recorded and the grace continues — the process-liveness check in
        watchdog_tick will classify a truly dead process as CRASHED on its
        own. Never raises, never touches execution_state directly."""
        if rt.transport is None:
            return
        try:
            rt.transport.send_abort(timeout=rt.thresholds.rpc_timeout_seconds)
        except Exception as exc:
            self._record_event(task_id, "watchdog", "stall_abort_failed", None, None,
                               summary=bound(str(exc)))
            return
        self._record_event(
            task_id, "watchdog", "stall_abort_sent", None, None,
            summary={
                "grace_seconds": rt.thresholds.stall_grace_seconds,
                "note": "RPC abort requested; settling during the grace "
                        "preserves the true result, ignoring it finalizes "
                        "STALLED",
            },
        )

    def _mark_unresponsive(self, task_id: str, rt: _TaskRuntime, current: str) -> None:
        """Mark UNRESPONSIVE after a failed heartbeat, remembering the
        pre-degradation state so a later real proof of liveness (event or
        changed heartbeat) can heal back to it (RUNNING, or TOOL_RUNNING if
        a tool was active)."""
        with rt.lock:
            if rt.pre_unresponsive_state is None:
                rt.pre_unresponsive_state = current
        self._set_execution_state(task_id, EXEC_UNRESPONSIVE)

    def _apply_changed_heartbeat(self, task_id: str, rt: _TaskRuntime, current: str) -> None:
        """Bookkeeping for a successful heartbeat whose state hash CHANGED
        since the last get_state: real progress/liveness, not mere RPC
        responsiveness. Clears the stall grace and heals degraded labels,
        mirroring what a real event does in ``_on_event`` — an unchanged
        heartbeat must never reach here and must never defeat true-stall
        detection. (``last_state_at``/``last_state_hash`` and the
        progress-refreshing fields were already persisted by
        ``_apply_state_snapshot``.)"""
        with rt.lock:
            rt.stall_grace_started_at = None
        if current in (EXEC_WAITING, EXEC_STALLED):
            self._set_execution_state(task_id, EXEC_RUNNING)
        elif current == EXEC_UNRESPONSIVE:
            # Mirror the event-based UNRESPONSIVE recovery in ``_on_event``:
            # a successful heartbeat with a CHANGED state hash is real
            # liveness/progress proof, so it must heal UNRESPONSIVE exactly
            # like a real event does — back to whatever active state
            # preceded it (RUNNING, or TOOL_RUNNING if a tool was active).
            with rt.lock:
                recovered_state = rt.pre_unresponsive_state or EXEC_RUNNING
                rt.pre_unresponsive_state = None
            self._set_execution_state(task_id, recovered_state)

    def _apply_state_snapshot(self, task_id: str, state: Dict[str, Any]) -> bool:
        """Persist one ``get_state`` snapshot's bounded fields plus its
        deterministic hash. Always updates ``last_state_at``. Only updates
        ``last_state_changed_at`` and ``last_progress_at`` (i.e. counts as
        progress/liveness) when the hash actually differs from the
        previously stored hash — an unchanged heartbeat must never refresh
        progress. Returns True iff the hash changed."""
        row = self.registry.get_task(task_id)
        previous_hash = row.get("last_state_hash") if row else None
        state_hash = compute_state_hash(state)
        now = self._now()
        updates: Dict[str, Any] = {
            "last_state_at": now,
            "last_state_hash": state_hash,
            "message_count": state.get("messageCount"),
            "is_streaming": 1 if state.get("isStreaming") else 0,
            "is_compacting": 1 if state.get("isCompacting") else 0,
        }
        changed = state_hash != previous_hash
        if changed:
            updates["last_state_changed_at"] = now
            updates["last_progress_at"] = now
        self.registry.update_task(task_id, **updates)
        return changed

    def _heartbeat(self, task_id: str, rt: _TaskRuntime):
        if rt.transport is None:
            return False, {}, False
        try:
            state = rt.transport.get_state(timeout=rt.thresholds.rpc_timeout_seconds)
            changed = self._apply_state_snapshot(task_id, state)
            return True, state, changed
        except (RpcTimeoutError, RpcTransportClosed, RuntimeError) as exc:
            self._record_event(task_id, "watchdog", "heartbeat_failed", None, None,
                                summary=str(exc))
            return False, {}, False

    def _live_descendants(self, rt: "_TaskRuntime", limit: int = 6) -> List[Dict[str, Any]]:
        """List the agent's live descendant processes, newest CPU time first.

        A stall is usually easy to explain and hard to see. On 2026-08-28 a
        task sat STALLED for seven minutes because a `lsof -nP -iTCP:... | head`
        never returned; Pi had already emitted tool_execution_end for it, so
        active_tool was empty and nothing in the registry hinted that a child
        process was still hanging around. Finding it took a manual walk of the
        process table. This makes that walk part of the stall record.

        Best effort by design: any failure returns an empty list rather than
        disturbing a task. Commands are truncated — this is a hint for a human,
        not a payload.
        """
        pid = getattr(rt.transport, "pid", None) if rt.transport else None
        if not pid:
            return []
        try:
            out = subprocess.run(
                ["ps", "-Ao", "pid=,ppid=,etime=,time=,command="],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return []
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) != 5:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3], parts[4]))
            except ValueError:
                continue
        by_parent: Dict[int, List[tuple]] = {}
        for r in rows:
            by_parent.setdefault(r[1], []).append(r)
        found, seen, stack = [], set(), [int(pid)]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for child in by_parent.get(cur, []):
                found.append({
                    "pid": child[0],
                    "elapsed": child[2],
                    "cpu_time": child[3],
                    "command": bound(child[4], 160),
                })
                stack.append(child[0])
            if len(found) >= limit:
                break
        return found[:limit]

    def _tool_process_active(self, rt: "_TaskRuntime", window: float) -> Optional[Dict[str, Any]]:
        """Proof of work from the OS, for when a tool is silent but busy.

        A long `nx run-many` emits nothing on stdout for minutes while
        genuinely compiling; treating that silence as a stall kills correct
        work. Walk the agent's process tree and compare accumulated CPU time
        against the previous sample: if it advanced, the tool is working.

        Returns a bounded evidence dict, or None when no activity could be
        proven. `None` is "no proof", NOT "proven idle" — the caller must
        fall through to the normal diagnostic path rather than treating an
        absent reading as a stall. Uses `ps` only: no new dependency, and a
        failure here must never fail a task.
        """
        # The OS pid lives on the transport (PiRpcTransport.pid), not on the
        # runtime record.
        pid = getattr(rt.transport, "pid", None) if rt.transport else None
        if not pid:
            return None
        try:
            out = subprocess.run(
                ["ps", "-Ao", "pid=,ppid=,time="],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return None

        children: Dict[int, int] = {}
        rows: List[tuple] = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2].strip()))
            except ValueError:
                continue
        by_parent: Dict[int, List[tuple]] = {}
        for cpid, ppid, cput in rows:
            by_parent.setdefault(ppid, []).append((cpid, cput))

        def secs(t: str) -> float:
            # ps TIME is [[dd-]hh:]mm:ss
            try:
                head, _, sec = t.rpartition(":")
                if "-" in head:
                    days, _, head = head.partition("-")
                    return int(days) * 86400 + int(head or 0) * 3600 + float(sec)
                if ":" in head:
                    hh, _, mm = head.partition(":")
                    return int(hh) * 3600 + int(mm) * 60 + float(sec)
                return int(head or 0) * 60 + float(sec)
            except Exception:
                return 0.0

        # Include the root process itself, not only its descendants: the
        # agent may be burning CPU directly (token generation) with no child
        # at all, and a descendants-only sum reports that as idle.
        own = {cpid: cput for cpid, _ppid, cput in rows}
        total = secs(own.get(int(pid), "0:00"))
        seen = set()
        stack = [int(pid)]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for cpid, cput in by_parent.get(cur, []):
                total += secs(cput)
                children[cpid] = 1
                stack.append(cpid)

        now = self._now()
        prev_cpu = getattr(rt, "_tool_cpu_total", None)
        prev_at = getattr(rt, "_tool_cpu_at", None)
        rt._tool_cpu_total = total
        rt._tool_cpu_at = now
        if prev_cpu is None or prev_at is None:
            return None  # first sample establishes the baseline only
        delta = total - prev_cpu
        if delta > 0.5 and (now - prev_at) <= window * 3:
            return {"cpu_delta_seconds": round(delta, 2),
                    "sample_gap_seconds": round(now - prev_at, 1),
                    "descendants": len(children)}
        return None

    def _diagnostic_snapshot(self, task_id: str, rt: _TaskRuntime, state_payload: Dict[str, Any]) -> None:
        # Exclude the watchdog's own rows: a snapshot that echoes previous
        # snapshots is not evidence the agent did anything.
        events = [
            e for e in self.registry.recent_events(task_id, limit=40)
            if e["event_type"] != "diagnostic_snapshot"
        ][:10]
        snapshot = {
            "recent_events": [
                {"type": e["event_type"], "ts": e["ts"]} for e in events
            ],
            "state": {
                k: state_payload.get(k)
                for k in ("isStreaming", "isCompacting", "messageCount", "pendingMessageCount")
            },
            "process_alive": rt.transport.poll() is None if rt.transport else False,
        }

        # The watchdog ticks every ~30 s, so a task that stays quiet writes an
        # identical row every tick: on 2026-08-28 pi-2c445bccbdf7 produced a
        # wall of snapshots reporting the same messageCount=53, isStreaming=1,
        # process_alive=true. Repetition carries no diagnostic information the
        # first row does not already carry, so an unchanged snapshot is written
        # at most once per snapshot_repeat_seconds. A CHANGED snapshot is
        # always written immediately — that is the row worth having.
        signature = json.dumps(
            {"state": snapshot["state"],
             "process_alive": snapshot["process_alive"],
             "recent": [e["type"] for e in snapshot["recent_events"]]},
            sort_keys=True, default=str)
        now = self._now()
        with rt.lock:
            unchanged = signature == rt.last_snapshot_sig
            last_at = rt.last_snapshot_at
            if unchanged and last_at is not None and (
                    now - last_at < rt.thresholds.snapshot_repeat_seconds):
                return
            rt.last_snapshot_sig = signature
            rt.last_snapshot_at = now

        if unchanged:
            snapshot["repeat"] = True
        self._record_event(task_id, "watchdog", "diagnostic_snapshot", None, None, snapshot)

    # -- hard timeout / abort --------------------------------------------

    def _emergency_cap_abort(self, task_id: str) -> None:
        """Infrastructure backstop, never a normal outcome. Distinct reason so
        `EMERGENCY_CAP_EXCEEDED` is never read as STALLED or CRASHED."""
        self.abort_task(task_id, reason="EMERGENCY_CAP_EXCEEDED")

    def abort_task(self, task_id: str, reason: str = "manual_abort") -> Dict[str, Any]:
        """Bounded cancellation. ABORTED means cleanup is confirmed, not requested.

        abort_lock linearizes prompt admission, transport publication and cleanup
        per task. Spawn is outside it; boot_pending retains execution ownership
        while the late process can still arrive. No global/SQLite lock spans I/O.
        """
        rt = self._rt(task_id)
        lock = rt.abort_lock if rt else threading.RLock()
        with lock:
            row = self.registry.get_task(task_id)
            if row is None:
                raise KeyError(task_id)
            if row['execution_state'] in EXEC_FINAL_STATES:
                return {'task_id': task_id, 'execution_state': row['execution_state'],
                        'already_final': True}
            self.registry.update_task(task_id, abort_requested=1)
            self._stop_feedback(task_id)
            self._record_event(task_id, 'manager', 'abort_requested', row['execution_state'],
                               None, summary={'reason': reason})
            self._stop_watchdog(task_id)
            if rt is None or (rt.boot_pending and rt.transport is None):
                # Never claim to have stopped an unobserved process. The boot
                # owner reconciles cancellation as soon as spawn returns.
                return {'task_id': task_id, 'execution_state': row['execution_state'],
                        'cancellation_pending': True}
            return self._complete_abort(task_id, rt, reason)

    def _complete_abort(self, task_id: str, rt: _TaskRuntime, reason: str) -> Dict[str, Any]:
        """Caller owns abort_lock; only the boot owner may call without transport."""
        transport = rt.transport
        rt.cleanup_pending = True
        try:
            if transport is not None and transport.poll() is None:
                try:
                    transport.send_abort(timeout=rt.thresholds.rpc_timeout_seconds)
                except Exception:
                    pass  # RPC failure must not skip process-level escalation.
                if transport.poll() is None:
                    transport.terminate()
                    if transport.wait(timeout=rt.thresholds.terminate_grace_seconds) is None:
                        transport.kill()
                        transport.wait(timeout=rt.thresholds.terminate_grace_seconds)
                if transport.poll() is None:
                    raise RuntimeError('process exit not confirmed after abort/terminate/kill')
        except Exception as exc:
            error = bound(f'abort cleanup pending: {exc}')
            self.registry.update_task(task_id, last_error=error)
            self._record_event(task_id, 'manager', 'abort_cleanup_pending', summary={'error': error})
            row = self.registry.get_task(task_id) or {}
            return {'task_id': task_id, 'execution_state': row.get('execution_state'),
                    'cancellation_pending': True, 'error': error}
        rt.cleanup_pending = False
        self._stop_watchdog(task_id)
        self._stop_feedback(task_id)
        self._set_execution_state(task_id, EXEC_ABORTED,
                                  exit_code=transport.poll() if transport else None,
                                  last_error=bound(f'aborted: {reason}'))
        self._notify_failed(task_id)
        return {'task_id': task_id, 'execution_state': (self.registry.get_task(task_id) or {}).get('execution_state'),
                'cancellation_pending': False}

    def steer_task(self, task_id: str, message: str) -> Dict[str, Any]:
        rt = self._rt(task_id)
        row = self.registry.get_task(task_id)
        if row is None:
            raise KeyError(task_id)
        if row["execution_state"] in EXEC_FINAL_STATES:
            raise RuntimeError(f"cannot steer a final-state task ({row['execution_state']})")
        if rt is None or rt.transport is None:
            raise RuntimeError("task has no live RPC transport (not attached)")
        rt.transport.send_steer(message)
        self._record_event(task_id, "manager", "steer_sent", None, None, summary={"message_len": len(message)})
        return {"task_id": task_id, "steered": True}

    # -- status ----------------------------------------------------------

    _DELIVERY_SECRET_PATTERNS = (
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        re.compile(r"(?i)\b(token|secret|password|api[_-]?key)\b\s*[:=]\s*\S+"),
        re.compile(r"\bsk-[A-Za-z0-9-]{8,}"),
    )

    @classmethod
    def _scrub_delivery_text(cls, text: Any) -> str:
        out = bound(str(text or ""), 300)
        for pattern in cls._DELIVERY_SECRET_PATTERNS:
            out = pattern.sub("[redacted]", out)
        return out

    def _delivery_diagnostics(self, task_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """Notice + continuation state for a single read (issue #7, part B).

        Distinguishes execution/verification (the rest of this payload) from
        NOTICE delivery and CONTINUATION state. ``accepted`` means the host
        accepted the wake — it is NOT a finished answer; ``turn_finished_at``
        is the separate, later signal and stays null until it is recorded.
        Legacy rows without data read ``unknown``; a non-final task reads
        ``not_yet`` — neither is success. Pure read: creates no wake, claims
        nothing, runs no recovery, calls no model.
        """
        final = row["execution_state"] in EXEC_FINAL_STATES
        notice_row = self.registry.find_terminal_notification(task_id)
        if notice_row is not None:
            notice = {
                "status": notice_row.get("status") or "unknown",
                "kind": notice_row.get("kind"),
                "attempts": int(notice_row.get("attempts") or 0),
                "sent_at": notice_row.get("sent_at"),
                "last_error": self._scrub_delivery_text(notice_row.get("last_error")) or None,
            }
        elif final:
            notice = {"status": "unknown"}
        else:
            notice = {"status": "not_yet"}
        wake_state = row.get("wake_state")
        if wake_state:
            wake: Dict[str, Any] = {
                "state": wake_state,
                "attempts": int(row.get("wake_attempts") or 0),
                "accepted_at": row.get("wake_accepted_at"),
                "last_error": self._scrub_delivery_text(row.get("wake_last_error")) or None,
            }
        elif not row.get("continuation_enabled"):
            wake = {"state": "disabled"}
        else:
            wake = {"state": "unknown"}
        turn_finished_at = None
        if wake_state == "accepted":
            for event in self.registry.recent_events(task_id, limit=50):
                if event.get("event_type") == "wake_turn_finished":
                    turn_finished_at = event.get("ts")
        wake["turn_finished_at"] = turn_finished_at
        return {"notice": notice, "wake": wake}

    def status(self, task_id: str) -> Dict[str, Any]:
        row = self.registry.get_task(task_id)
        if row is None:
            raise KeyError(task_id)
        rt = self._rt(task_id)
        now = self._now()
        started_at = row.get("started_at") or now
        last_progress_at = row.get("last_progress_at") or started_at
        # Both ages below freeze once the task reaches a final state. Measuring
        # a finished task against "now" makes its reported runtime grow without
        # bound — an hour after it settled, a 154 s task reads as 3 700 s, and
        # two tasks that ran back to back read as overlapping because each one
        # is stretched to the moment of the report. ABORTED/CRASHED never get a
        # settled_at, so fall back to the last event we recorded for them.
        if row["execution_state"] in EXEC_FINAL_STATES:
            ended_at = row.get("settled_at") or row.get("last_event_at") or now
        else:
            ended_at = now
        return {
            "task_id": task_id,
            "executor": self._executor_metadata(row),
            "execution_state": row["execution_state"],
            "verification_state": row["verification_state"],
            "state": derived_state(row["execution_state"], row["verification_state"]),
            "runtime_seconds": round(max(ended_at - started_at, 0.0), 3),
            "pid": row.get("pid"),
            "session_id": row.get("session_id"),
            "last_event_type": row.get("last_event_type"),
            "last_progress_age_seconds": round(max(ended_at - last_progress_at, 0.0), 3),
            "active_tool": row.get("active_tool"),
            "message_count": row.get("message_count"),
            "is_streaming": bool(row.get("is_streaming")),
            "is_compacting": bool(row.get("is_compacting")),
            "settled_at": row.get("settled_at"),
            "exit_code": row.get("exit_code"),
            "last_error": row.get("last_error"),
            "cwd": row.get("cwd"),
            "delivery": self._delivery_diagnostics(task_id, row),
        }

    def digest(self, task_id: str) -> Dict[str, Any]:
        """Answer "what did this delegate actually do?" in a few KB.

        status() reports the state machine but says nothing about the work, so
        the only way to see what a delegate did was to read its session
        transcript — 93 KB for a short task, 991 KB for the largest one on
        disk. Reading one of those into the orchestrator costs far more context
        than the delegation saved, and on 2026-08-28 08:55 the agent tried
        exactly that.

        So the transcript is parsed HERE and only the shape of the work crosses
        back: the prompt, which tools ran and against what, and the delegate's
        own closing words. Every field is individually bounded, so the result
        stays small however long the task ran.
        """
        row = self.registry.get_task(task_id)
        if row is None:
            raise KeyError(task_id)

        out: Dict[str, Any] = {
            "task_id": task_id,
            "state": derived_state(row["execution_state"], row["verification_state"]),
            "execution_state": row["execution_state"],
            "verification_state": row["verification_state"],
            "cwd": row.get("cwd"),
            "executor": self._executor_metadata(row),
        }

        path = row.get("session_file")
        if not path or not os.path.isfile(path):
            out["digest_error"] = "no readable session file"
            return out

        prompt = ""
        final_text = ""
        counts: Dict[str, int] = {}
        assistant_turns = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # a half-written trailing line is normal
                    if entry.get("type") != "message":
                        continue
                    message = entry.get("message") or {}
                    role = message.get("role")
                    parts = message.get("content")
                    if not isinstance(parts, list):
                        continue
                    if role == "user" and not prompt:
                        prompt = _first_text(parts, DIGEST_PROMPT_CHARS)
                    elif role == "assistant":
                        assistant_turns += 1
                        text = _first_text(parts, DIGEST_FINAL_CHARS)
                        if text:
                            final_text = text  # keep overwriting; the last wins
                        for part in parts:
                            if isinstance(part, dict) and part.get("type") == "toolCall":
                                name = str(part.get("name") or "?")
                                counts[name] = counts.get(name, 0) + 1
        except OSError as exc:
            out["digest_error"] = f"session unreadable: {exc}"
            return out

        # The delegate's own closing words are the answer to "what did you
        # do?". An earlier version also rebuilt the last 25 tool calls with
        # their arguments; that was several KB of reconstruction to say what
        # the delegate had already said in a paragraph. The tool histogram
        # stays because it is one line and it distinguishes a delegate that
        # worked from one that only talked.
        out["prompt"] = prompt
        out["summary"] = final_text
        out["assistant_turns"] = assistant_turns
        out["tool_counts"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
        out["tool_calls_total"] = sum(counts.values())
        if row.get("last_error"):
            out["last_error"] = row.get("last_error")
        return out

    # -- recovery ------------------------------------------------------

    def recover_all(self) -> List[Dict[str, Any]]:
        """Inspect non-final registry rows and attempt recovery for each,
        PLUS any already-``SETTLED`` rows whose verification is still
        ``PENDING``/``NOT_RUN`` and carry a persisted verifier spec — those
        need their verifier re-run, not a live RPC reattach.

        Never sends a prompt. Never resumes on identity mismatch or a
        missing/empty session file — those are recorded as ABORTED with a
        bounded recovery error and the row is left non-resumed.
        """
        self._prune_registry()
        results = []
        for row in self.registry.list_tasks(execution_states=EXEC_RESUMABLE_STATES):
            results.append(self.recover_task(row["task_id"]))
        for row in self.registry.list_tasks(execution_states={EXEC_SETTLED}):
            if row.get("verification_state") in (VERIFY_PENDING, VERIFY_NOT_RUN) and row.get("verifier_spec"):
                results.append(self.recover_task(row["task_id"]))
                continue
            # Crash-safety for the wake itself: if the manager died between
            # the gate resolving (or never existing) and the wake request,
            # the row sits SETTLED with a resolved gate and no wake state.
            # The CAS inside _request_terminal_wake makes this boot-time
            # sweep safe to run on every start: at most one wake per task,
            # requested only when none exists yet.
            resolved_gate = (
                row.get("verification_state") in (VERIFY_PASS, VERIFY_FAIL, VERIFY_UNRUNNABLE)
                or (row.get("verification_state") == VERIFY_NOT_RUN
                    and not row.get("verifier_spec"))
            )
            if resolved_gate and row.get("wake_state") is None:
                self._request_terminal_wake(row["task_id"])
        return results

    def recover_task(self, task_id: str) -> Dict[str, Any]:
        # Loading a plugin in another CLI/Desktop/gateway must never reopen
        # a live Pi session. Exclusion also covers STARTING before it has a
        # PID, and settlement while the verifier is still running.
        if not self._claim_execution(task_id):
            return {'task_id': task_id, 'recovered': False, 'reason': 'execution_owner_active'}
        try:
            row = self.registry.get_task(task_id)
            if row and execution_owner.process_alive(row.get('pid')):
                self._release_execution(task_id)
                return {'task_id': task_id, 'recovered': False, 'reason': 'worker_process_active'}
            if (row and row['execution_state'] == EXEC_STARTING and row.get('pid') is None and row.get('session_file')
                    and 0 <= self._now() - (row.get('created_at') or 0) < 60):
                # A pre-lock host can briefly publish STARTING before its PID.
                self._release_execution(task_id)
                return {'task_id': task_id, 'recovered': False, 'reason': 'worker_starting'}
            result = self._recover_task_owned(task_id)
            if not result.get('recovered') or (row and row['execution_state'] == EXEC_SETTLED):
                self._release_execution(task_id)
            return result
        except BaseException:
            self._release_execution(task_id)
            raise

    def _recover_task_owned(self, task_id: str) -> Dict[str, Any]:
        row = self.registry.get_task(task_id)
        if row is None:
            raise KeyError(task_id)

        verifier = _load_verifier_spec(row.get("verifier_spec"))

        if row["execution_state"] == EXEC_SETTLED:
            # Already settled: there is nothing to reattach over RPC. The
            # only thing recovery can lose here is the verifier spec, so
            # restore and (if still pending) re-run it. execution_state
            # stays strictly SETTLED; only verification_state may change.
            return self._recover_settled_verification(task_id, row, verifier)

        expected_id = row.get("expected_session_id")
        expected_file = row.get("expected_session_file")
        session_file = row.get("session_file")

        def _reject(reason: str) -> Dict[str, Any]:
            self._set_execution_state(task_id, EXEC_ABORTED, recovery=True, last_error=bound(reason))
            self._record_event(task_id, "recovery", "recovery_rejected", row["execution_state"],
                                EXEC_ABORTED, summary={"reason": reason})
            self._notify_failed(task_id)
            # A RECOVERY failure is a terminal outcome the orchestrator must
            # act on (the task is dead and the reason is bounded); this is
            # deliberately distinct from a MANUAL abort, which never wakes.
            self._request_terminal_wake(task_id)
            return {"task_id": task_id, "recovered": False, "reason": reason}

        if not session_file:
            return _reject("recovery_missing_session: no session_file on record")
        path = Path(session_file).expanduser()
        if not path.exists() or path.stat().st_size == 0:
            return _reject("recovery_missing_session: session file absent or empty")

        try:
            spec = self._stored_executor(row)
        except ValueError:
            return _reject("recovery_executor_missing_or_invalid: explicit operator reconciliation required")
        thresholds = self.default_thresholds
        try:
            process = self._spawn_process(task_id, row.get("cwd") or ".", session_file=str(path), executor_spec=spec)
        except Exception as exc:
            return _reject(f"recovery_spawn_failed: {exc}")

        transport = PiRpcTransport(
            process,
            on_event=lambda ev: self._on_event(task_id, ev, source="recovery_replay"),
            on_malformed=lambda line: self._on_malformed(task_id, line),
        )
        try:
            state = transport.get_state(timeout=thresholds.rpc_timeout_seconds)
        except (RpcTimeoutError, RpcTransportClosed, RuntimeError) as exc:
            transport.terminate()
            return _reject(f"recovery_get_state_failed: {exc}")

        executor_error = self._executor_identity_error(row, state)
        if executor_error:
            transport.terminate()
            return _reject('recovery_' + executor_error)

        # Identity validation happens BEFORE any registry write — the
        # stored "expected" values are ground truth and must never be
        # clobbered by an unverified actual value ahead of this check.
        actual_id = state.get("sessionId")
        actual_file = state.get("sessionFile")
        identity_ok = True
        if expected_id and actual_id != expected_id:
            identity_ok = False
        if expected_file:
            # Reject when the actual sessionFile is missing OR differs from
            # the expected one, comparing canonical/expanded absolute paths
            # (not raw strings, and not "falsy actual => skip check").
            if not actual_file or _canonical_path(actual_file) != _canonical_path(expected_file):
                identity_ok = False
        if actual_id is None or (state.get("messageCount") in (None, 0) and not expected_id):
            # A brand-new empty session must never be silently adopted.
            if not expected_id:
                identity_ok = False

        if not identity_ok:
            transport.terminate()
            return _reject(
                f"recovery_identity_mismatch: expected session_id={expected_id!r} "
                f"got {actual_id!r}"
            )

        rt = _TaskRuntime(task_id=task_id, thresholds=thresholds, verifier=verifier, now_fn=self._now)
        rt.transport = transport
        with self._runtimes_lock:
            self._runtimes[task_id] = rt
        self.registry.update_task(
            task_id, pid=getattr(process, "pid", None), session_id=actual_id,
            session_file=actual_file,
        )
        if verifier is not None:
            self._record_event(task_id, "recovery", "verifier_restored", None, None,
                                summary={"argv": verifier.argv})

        # Cursor-based replay: get_entries(since=last_entry_id).
        since = row.get("last_entry_id")
        try:
            entries_resp = transport.get_entries(since=since, timeout=thresholds.rpc_timeout_seconds)
        except (RpcTimeoutError, RpcTransportClosed, RuntimeError) as exc:
            self._record_event(task_id, "recovery", "get_entries_failed", None, None, str(exc))
            entries_resp = {}

        entries = entries_resp.get("entries") or []
        for entry in entries:
            self._record_event(task_id, "recovery_replay", "entry_replayed", None, None,
                                summary={"entry_id": entry.get("id")} if isinstance(entry, dict) else None)
        leaf_id = entries_resp.get("leafId")
        if leaf_id:
            self.registry.update_task(task_id, last_entry_id=str(leaf_id))

        # Simple restart rule: the task/process is alive and we just probed
        # it (get_state above), so persist the probed snapshot for a live
        # baseline, establish a FRESH last_progress_at=now, and resume
        # monitoring. Downtime itself must never count as a stall.
        self._apply_state_snapshot(task_id, state)
        # Fresh runtime => fresh progress baseline: no progress notice may
        # fire on the recovered task until its signature actually moves.
        self._seed_progress_baseline(task_id, rt)
        self._set_execution_state(task_id, EXEC_WAITING, recovery=True,
                                  abort_requested=0, last_progress_at=self._now())
        self._record_event(task_id, "recovery", "recovery_attached", row["execution_state"],
                            EXEC_WAITING, summary={"session_id": actual_id, "replayed": len(entries)})
        self._start_watchdog(task_id)
        return {"task_id": task_id, "recovered": True, "session_id": actual_id, "replayed_entries": len(entries)}

    def _recover_settled_verification(
        self, task_id: str, row: Dict[str, Any], verifier: Optional[VerifierSpec],
    ) -> Dict[str, Any]:
        """Restore and (if still pending) re-run the verifier for a task
        that reached SETTLED before the manager restarted. Never derives
        DONE without an actual verifier PASS, and never touches
        execution_state — only verification_state may change here."""
        if verifier is None or row.get("verification_state") not in (VERIFY_PENDING, VERIFY_NOT_RUN):
            return {
                "task_id": task_id, "recovered": True, "execution_state": EXEC_SETTLED,
                "verifier_restored": False, "verification_run": False,
            }
        rt = _TaskRuntime(task_id=task_id, thresholds=self.default_thresholds,
                           verifier=verifier, now_fn=self._now)
        with self._runtimes_lock:
            self._runtimes[task_id] = rt
        self._record_event(task_id, "recovery", "verifier_restored", None, None,
                            summary={"argv": verifier.argv})
        outcome = self.run_verifier_now(task_id, verifier)
        # Announce after recovery re-ran the gate: if the original process
        # died between settlement and delivery, the stable 'verifier'
        # notification id makes this either the delivery (nothing enqueued
        # yet) or a local dedupe (it was already enqueued or sent).
        self._notify_done(task_id)
        return {
            "task_id": task_id, "recovered": True, "execution_state": EXEC_SETTLED,
            "verifier_restored": True, "verification_run": True, "verification_state": outcome,
        }

    def shutdown(self) -> None:
        """Bounded teardown for the end of the plugin's process life.

        Sets the shutdown flag (from now on ``_start_watchdog`` refuses, so
        a still-booting task cannot resurrect a watchdog), stops every
        watchdog and joins them, each with a bounded timeout: a watchdog
        blocked inside its RPC heartbeat returns within the bounded
        window, and a join that does time out is harmless — the thread is
        a daemon, and the registry's post-close no-op guard
        (``registry_db._closed_guard``) keeps any of its late writes
        exception-free. Never kills a task's RPC process: an orphaned
        process is exactly what recovery is designed to re-attach to on
        the next boot, and killing at teardown would change outcomes.
        Idempotent and safe to call more than once.
        """
        self._shutdown_flag.set()
        if self._activity_recorder is not None:
            try:
                self._activity_recorder.close()
            except Exception:
                logger.warning("Pi live view shutdown failed", exc_info=True)
        with self._runtimes_lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            self._stop_feedback(rt.task_id)
            rt.stop_watchdog.set()
        for rt in runtimes:
            thread = rt.watchdog_thread
            if (thread is not None and thread.is_alive()
                    and thread is not threading.current_thread()):
                thread.join(timeout=2.0)
        for task_id in list(self._execution_locks):
            self._release_execution(task_id)


def _canonical_path(value: Optional[str]) -> Optional[str]:
    """Canonical, expanded, absolute form of a path string for identity
    comparison — never compare raw strings, since equivalent paths can
    differ in ``~`` expansion, trailing slashes, or relative segments."""
    if not value:
        return None
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return str(Path(value).expanduser())


def _load_verifier_spec(raw: Optional[str]) -> Optional["VerifierSpec"]:
    """Reconstruct a persisted verifier spec (see ``start_task``'s
    ``verifier_spec`` JSON column) into a runtime ``VerifierSpec`` so
    recovery can restore it — without this, a task's verifier is silently
    lost across a manager restart."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    argv = data.get("argv")
    if not argv:
        return None
    return VerifierSpec(argv=list(argv), timeout_seconds=float(data.get("timeout_seconds") or 120.0))


def _bounded_event_summary(event: Dict[str, Any]) -> Dict[str, Any]:
    """A bounded, non-token-firehose summary of one RPC event for the
    append-only events table. Deliberately drops large text payloads."""
    keep_keys = (
        "type", "willRetry", "tool", "toolName", "toolCallId", "requestId",
        "entryId", "id", "turnId",
    )
    summary = {k: event.get(k) for k in keep_keys if k in event}
    for text_key in ("text", "content", "message"):
        value = event.get(text_key)
        if isinstance(value, str):
            summary[f"{text_key}_len"] = len(value)
    return summary
