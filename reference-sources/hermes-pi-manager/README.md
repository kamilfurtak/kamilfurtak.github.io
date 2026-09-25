# pi-manager

A Hermes plugin that delegates work to the [Pi](https://github.com/badlogic/pi-mono) coding agent
and supervises it — durably, without blocking, and without spending the
orchestrator's turns.

Pi runs as a long-lived RPC subprocess. The plugin owns its lifecycle: a
SQLite registry that survives restarts, a layered stall watchdog, a separate
verification step, a passive notification rail, and exactly **one** wake back
into the session that dispatched the task.

## Why not `delegate_task` or the subagent lifecycle API?

Hermes' native subagent API manages **in-process** children. Pi is an external
process, so it is out of scope for that API — and the API is weaker on the axes
this plugin exists for: handles do not survive a restart, per-launch timeouts
are rejected, there are no hooks for observing intermediate progress, and
`wait()` blocks. Blocking is the thing to avoid: a blocked turn is a burnt turn.

## Design invariants

**Zero agent turns for anything but the result.** A durable outbox sends
messaging notices through the host's `send_message_tool`; ordinary Desktop
chats receive native in-app notices. Neither path invokes a model or registers
a `send_message` tool. Desktop notices replace one toast per task and expire
after 20 seconds; they are not persistent transcript lines or OS alerts.

**One wake per task, carrying the verdict.** When a task reaches a terminal
state *after* verification, one continuation is requested for its originating
session. The wake
carries the gate result and the semantic check, so a clean outcome needs no
follow-up call at all:

```
Task `pi-abc` reached its terminal state (execution_state=SETTLED,
verification_state=PASS). Gate: gate passed (exit 0). Semantic check: no LSP
errors detected in 4 touched file(s). This is the complete outcome — continue the
parent workflow autonomously without re-reading the task.
```

**Semantic verification Pi cannot do itself.** Pi ships no language server.
After settlement the plugin runs the host's LSP servers over the files the task
touched (`git diff` + untracked) and folds the result into the same wake. This
is an in-process call — zero tokens.

**Execution and verification are distinct axes.** `execution_state` says
whether the agent finished; `verification_state` says whether the gate passed.
A task can settle cleanly and still fail its gate.

**Crash-safety over convenience.** A wake dispatch whose owner died, or whose
Desktop admission raised after a possible start, becomes `uncertain` and is
never automatically retried. A live dispatcher is preserved when another Hermes
process scans the shared registry. Acceptance is distinct from finishing the
turn; Desktop records the latter as a `wake_turn_finished` event.

## WebUI delivery and cancellation

See [WebUI delivery and startup cancellation](docs/webui-delivery-and-cancellation.md)
for surface routing, passive-notice limitations, safe cancellation during spawn,
concurrent-task isolation and the separate live rollout acceptance gate.

## Live activity in Desktop

The optional Desktop half renders `::pi-live{task="pi-…"}` as an updating card
inside the assistant's start acknowledgement. `pi_task` supplies that directive
for native Desktop/TUI origins. A Desktop with the frontend installed renders
it; other clients retain their existing notices and terminal continuation.

Expand **Pokaż przebieg** to follow the same chronological log as the CLI:
timestamps, executed commands/arguments, visible Pi messages, incremental output
from each tool, durations and errors. Desktop uses its native `LogView` component.
New complete lines arrive about once a second, including while the parent
conversation is idle. Reading earlier lines pauses auto-scroll; **Do najnowszych**
resumes it. Collapsing the card stops downloading log content; the compact status,
latest message preview and completed-tool counter continue to refresh.

The expanded view requests only new bytes after its last cursor, retains up to
256 KiB of recent text and explicitly marks a reset after rotation or a display
limit. Repeated cumulative RPC results do not duplicate output. A navigation or
connection change discards in-flight replies. Polling ends only when the
backend CONFIRMS the final publication: the recorder stamps the snapshot with
`publication: {state: "complete", seq}` once the task reaches a terminal state,
and the log carries a `final` marker written after its last append. A viewer
that already saw "complete" resumes automatically when newer events reopen the
stream. A failed snapshot or log write retains the affected version and is
retried (bounded backoff), so a newer version is never silently dropped.
Older backends without the markers get a bounded refresh and are then shown as
"Podgląd może być niepełny" with a manual **Odśwież** — the card never stops on
two identical reads, so a publication delayed past identical responses is still
fetched. This is a bounded display log, not an unbounded raw Pi session:
private thinking is excluded and complete lines are redacted before publication.

Older workers/backends without the new transcript retain the existing formatted
recent-message view (eight history entries, foldable completed tool results and
foreground-tool output). The counter always counts completed tool calls,
including overlapping calls, not messages or an estimated percentage.
Execution and verification stay separate.

Buttons, status dots and tool logs use the corresponding Desktop SDK components;
older SDKs retain the text/HTML fallback. The collapsed preview starts at the
beginning of the latest Pi message. This is a plugin transcript contribution:
Desktop does not expose its built-in subagent roster as an external provider API.

Install the Python plugin on the backend as usual and reload that backend after
updating it. On the computer running Desktop, copy `desktop/plugin.js` into
`~/.hermes/desktop-plugins/pi-manager/plugin.js`, then use **Settings → Plugins**
or **⌘K → Reload desktop plugins**. This reload replaces frontend contributions
without restarting the backend. No frontend build or npm install is needed.
If both halves run on one computer, the existing unified plugin directory is
also discovered; enable its Desktop half in Settings.

For Desktop over SSH, the JS file belongs on the laptop and the Python files
belong on the remote host. `ctx.rest` uses Desktop's existing authenticated
connection to native `hermes serve`; no additional listener or external WebUI
is required. The read-only endpoint rejects tasks from another conversation or
profile, supports compression continuations, and never initializes a manager.
The frontend drops in-flight responses when navigation changes its owner.

`state/pi-manager/activity/` contains disposable, redacted, private snapshots.
One coalescing writer per manager publishes them atomically; event handling
never performs network or filesystem I/O for the view. Snapshots expire after
seven days and are capped at 256 files; each is at most 128 KiB. A presentation
failure does not change task settlement, verification, notices or wake policy.
Older tasks without snapshots still display their registry status.

Validation: `python -m unittest discover -s tests`; for the React view,
`cd desktop && npm ci && npm test`. The latter dependencies are only for tests;
Desktop supplies React and the SDK at runtime.

## Requirements

- Hermes Agent **v0.21.0+** — earlier hosts have no `session_key` on
  `inject_message`, so terminal continuation is disabled with a logged error
  (the rest of the plugin still works).
- The `pi` binary on `PATH`. Without it the tools are hidden from the model
  entirely rather than offered and failing.
- For terminal continuation, the gateway-injection grant below.
- Ordinary Desktop/TUI support requires the native backend contract checked by
  `desktop_host.py` (verified on Hermes 0.21.1, cores `b2aa855b` and `13c58042`). A missing
  contract leaves delivery pending; it never redirects the result elsewhere.

## Install

```bash
hermes plugins install kamilfurtak/hermes-pi-manager --enable
```

Pin a revision for reproducibility:

```bash
hermes plugins install kamilfurtak/hermes-pi-manager --ref <full-commit-sha> --enable
```

## Required configuration

Terminal continuation needs an explicit grant. It is a plain config flag, not
a declared capability, so **the install flow will not prompt for it** — without
this the wake is refused and tasks end in `wake_exhausted`:

```yaml
plugins:
  entries:
    pi-manager:
      allow_gateway_injection: true
```

## Executor profiles

See [approved executor profiles](docs/executor-profiles.md) for task-local
provider/model/thinking selection, durable recovery identity, validation gates
and staged rollout. With no profile file, existing local defaults remain unchanged.

## Managed-context isolation

Every process this plugin spawns runs with managed-context isolation
(`rpc_transport.MANAGED_CONTEXT_FLAGS` + an explicit empty `--system-prompt`),
applied identically to normal starts and to recovery:

- `--no-context-files` — no AGENTS.md/CLAUDE.md discovery (global, ancestor,
  project); `--no-skills` — no skill discovery; `--no-extensions` — no
  extension discovery (including settings/package-provided extensions);
  `--no-prompt-templates` — no prompt-template discovery. `-e`/`--skill`-style
  explicit paths, if ever needed, remain the only allowlist mechanism.
- The empty `--system-prompt` value is an explicit "no SYSTEM.md": the loader
  only falls back to `SYSTEM.md` discovery when the CLI value is absent, and
  an empty value resolves to "no custom prompt" — so the default base
  preamble is kept while global or trusted-project `SYSTEM.md` can never
  inject. (`APPEND_SYSTEM.md` discovery is already skipped whenever
  `--append-system-prompt` is passed, which managed runs always do.)

The leaf-worker policy is REQUIRED and fail-closed: the packaged
`pi-worker.md` ships with the plugin (canonical source for managed runs); an
explicit `system_prompt_file` override must also be a REGULAR file (enforced
with fstat on the fd that is actually opened — a FIFO, device or socket is
rejected before any read, the open is non-blocking so a writer-less FIFO
cannot stall the boot path, a symlink to a regular file stays allowed, and
the read is bounded at 256 KiB) with valid UTF-8 and non-empty content. A
relative override resolves against the MANAGER's cwd (documented base) and
the source path is resolved absolute.

The validated bytes are written once to a manager-owned, content-addressed
SNAPSHOT (`<state>/policy-snapshots/<sha256>.md`, mode 0600): first creation
wins via an `O_EXCL` temp file published with `os.link`, which can never
replace an existing address, so two managers racing on a first creation
converge on one verified inode. An EXISTING snapshot is reused only after
re-hashing against its address; a mismatched, special (FIFO/device,
rejected through the same fd guard), oversize or unreadable file is a
controlled error BEFORE any spawn and is left untouched for inspection —
only a real absence authorises creating a new snapshot (no auto-repair).
The child receives the absolute snapshot path. Raw text cannot be used here: pi resolves `--append-system-prompt` as
a FILE whenever a file with that name exists (verified against v0.86.0
`resolvePromptInput` → `existsSync(input)`), so even a validated short
override could be silently reinterpreted against the task cwd. The snapshot
removes that ambiguity and is independent of later replacement or removal of
the source; the recorded sha256 covers exactly the snapshot's bytes. A
missing, unreadable, non-regular, empty or mis-encoded policy aborts the
spawn with a clear error before any process exists; a bogus path never
reaches argv.

The binary compatibility gate probes the binary once per identity
(`--version` plus `--no-extensions --help`) with a temporary EMPTY
`PI_CODING_AGENT_DIR` and cwd — process-local only, never the manual CLI or
global state. That isolation matters because pi builds its runtime (and would
EXECUTE discovered extensions) before printing `--help`. The probe verifies
only a plausible version and the presence of the four flags; the empty
`--system-prompt` suppression semantics is verified by the real-loader suite,
not by the probe. Unsupported or unreadable runtimes refuse to start with the
reason kept visible — never a silent full-context fallback.

Every spawn ATTEMPT records an `isolation_applied` event (existing event
mechanism, no schema change) with the isolation id, the flag set, the policy
source path, the snapshot path, its sha256 prefix and
`delivery: snapshot-path`, plus the pi
path and version. This is configuration evidence recorded before Popen — it
does not by itself prove the process ran.

Manual `pi` usage is untouched: `~/.pi/agent/prompts/pi-worker.md` (a
workspace-owned copy) stays a manual-use template only. Real-loader proof
lives in the opt-in `pi` suite (`scripts/run-tests.py --suite pi`): control
loads every marker and isolation blocks every marker (also in a nested git
worktree and with `--approve`), the real MANAGER is driven through probe,
start and recovery with a cold probe cache and recorded argv (extension code
must never execute in the probe or the RPC run), the RPC contract
(`get_state`/`get_entries`/`agent_settled`) is exercised under isolation, and
the manual `/pi-worker` template contract (frontmatter parsed, metadata never
expanded) is pinned.

## Tools

| Tool | Purpose |
|---|---|
| `pi_task` | Start one Pi task; returns immediately |
| `pi_status` | Current registry state for a task |
| `pi_digest` | ~2 KB account of what a task DID, instead of its transcript |
| `pi_abort` | Kill switch: RPC abort → SIGTERM → SIGKILL |
| `pi_steer` | Send a steer command to a live task |
| `pi_resume` | Re-run the recovery algorithm for one task |
| `pi_verify` | Run the verification step once a task has settled |

There is deliberately no blocking wait tool.

## Delivery by host

| Host | Passive progress | Terminal continuation |
|---|---|---|
| Telegram/gateway | Native messaging adapter; latest visible stage and completed tool count | Existing `inject_message(session_key=...)` |
| Classic interactive CLI | Native subagent dock and live inspector; passive text fallback on older hosts | Owning CLI's normal FIFO, after the parent is idle |
| Ordinary Desktop/TUI | `notification.show` to the session's native transport | Native prompt admission in that same backend |

The CLI adapter adds Pi rows to the existing `SubagentMonitor` instance. It uses
the host's dock, theme, fullscreen viewer and keybindings: **Ctrl+T / F6** opens
the roster, **Enter** opens the selected live tail, **Esc** returns, and **F7**
collapses the dock. Native subagents retain their original rows and controls.
**s** queues guidance to Pi; **x**, then the native confirmation, stops it.
Opening/closing the viewer preserves the composer draft and does not stop Pi.
After successful attachment, `pi_task` supplies a short, nonempty acknowledgement
that the CLI presents through the dock instead of a separate response box. Empty
model replies trigger Hermes' retry guard, so the model still ends its turn with
the supplied text. A reversible adapter consumes only that exact acknowledgement
in the dispatching turn, including streamed tokens; history retains it. Other
answers, errors, explicit task-ID requests and the terminal continuation remain
ordinary responses. Bindings are released after the turn, including interruption.
Older hosts and voice/TTS retain a short visible acknowledgement when quiet
presentation is unavailable. This does not change Hermes' conversation loop.
The native spinner refreshes activity about once a second even while the parent
is idle or working on another request. Once no agents remain it stops repainting
the idle prompt. No model turn is used for monitoring.

Only the owning CLI process and conversation (or its compression tip) receive
Pi rows and notices. `/new`, a closed CLI or a foreign process cannot receive
them. When the native view is available, progress receipts do not also print
repeated messages above the prompt. Terminal notices still use the native
renderer, but wait until the inspector releases the terminal. They never cancel
prompt_toolkit's shared terminal queue on a delivery timeout. This prevents a
finished task from leaving a stale dock and hiding later conversation output.
On older hosts the existing passive text notices remain available.
Wakes wait for the parent, queued user input and native modals/inspector to clear;
they enter the normal FIFO, never the interrupt queue. A scope guard checks the
conversation again when a queued wake is consumed. A session switch can discard
an already admitted wake; it is not replayed into a different conversation.
CLI queue states are isolated from the older workers’ pending/leased states,
so long-lived processes with an older plugin cannot claim local notices.
The CLI adapter captures identity when a new task is dispatched: reopen the
CLI process to load an updated plugin before testing. Reconnecting Herdr to the
same surviving process does not reload Python. Desktop frontend changes require
copying/reloading its JS half as described above.

All three views use the same bounded, redacted projection for compact status.
The CLI inspector and expanded Desktop card additionally read an append-only event log: timestamps,
executed tool arguments, visible assistant text, incremental tool output,
per-tool durations and errors. Cumulative RPC tool updates are reconciled by
call ID, so parallel tools and repeated snapshots do not duplicate output.
Completed lines appear while a tool runs; an unfinished line is held until its
newline or the end of the message/result, then redacted before publication.
Private thinking and speculative tool arguments never enter this log.

The native viewer follows the newest **32 KiB** and supports its normal scrolling;
it no longer receives a replacement of the last eight compact entries. The
private `state/pi-manager/cli-transcripts/<task-hash>.log` preserves more history,
rotating at 2 MiB into one `.log.1` previous part. Both are 0600, retained for
seven days/up to 256 task pairs. Oversize lines or a saturated display queue
produce explicit omission markers; display I/O cannot change task outcomes.
Existing tasks without this log retain their previous snapshot-based preview.

Native Telegram provides message delivery rather than a terminal-style live inspector;
the plugin does not impersonate a built-in subagent to manufacture one.
Passive notices remain rate-limited, and execution/verification remain separate.
Tool errors appear in live output; a failed command that Pi handles is not
automatically a failed task. Task failures and verifier outcomes use terminal
notices without waiting for the progress interval (delivery still requires a
live channel and the normal outbox drain).

Execution ownership is separate from notification ownership. A plugin-owned OS
lock protects each task from before STARTING through verification, so loading a
second Hermes host cannot reopen live Pi work. Legacy tasks with a live worker
PID are left alone; lock ownership is released on completion or process exit.
Recovery still validates session identity after an owner is gone. These are
Unix advisory locks alongside the registry, not changes to Hermes source.

Desktop waits until the current turn, queued human prompts and scheduled native
continuation are clear. An absent owner does not spend the retry budget or block
other sessions. Reopening a durable conversation (including its compression tip, in the same profile)
can make pending delivery eligible again. A reused UI tab is not sufficient
identity. The grant above also gates Desktop turns.

Restart/reconnect the affected Desktop backend and reload other long-lived
Hermes processes after upgrading this plugin. Existing Python processes retain
their loaded code: a host started before a delivery-routing change keeps the
old adapters in memory and may keep claiming other hosts' outbox rows, burning
their delivery attempts (symptom: repeated `Unknown or unregistered plugin
platform: webui` while a correctly deployed host sits idle — issue #7).
After such an upgrade restart every long-lived host, including open CLI
sessions, not just the desktop/webui/gateway processes. Check active work
before restarting; an already `accepted` or `uncertain` historic wake is not
automatically replayed by an upgrade.

## Host internals

These files call Hermes internals rather than the documented `PluginContext`
surface, because the public API exposes no equivalent:

- `host_adapter.py` → `tools.send_message_tool` — the passive notification rail.
- `lsp_check.py` → `agent.lsp.get_service` — semantic verification.
- `desktop_host.py` → the already loaded `tui_gateway.server` session table,
  native event transport and `_run_prompt_submit(..., terminal_callback=...)`.
  It also checks the existing injection-grant predicate. This is a guarded
  compatibility adapter, not official generic Desktop `inject_message` support.
- `wake_worker.py` reads the context's manager to determine whether this process
  actually has the gateway injector.
- `cli_host.py` → the owning CLI application and normal input FIFO, with scope
  checks at enqueue and consumption; passive notices use the native renderer.
- `cli_monitor.py` → instance-local adapters for native `SubagentMonitor.refresh`
  and `control`. The native UI/keybindings are reused; methods are restored on
  teardown. No native delegation registry entries or Hermes files are changed.
  Readable tails are private, redacted projections, not raw Pi session transcripts.
- `live_transcript.py` → a plugin-owned display log assembled from RPC events.
  It reuses the activity writer, preserves tool identity and incremental output,
  and keeps up to 256 task log pairs for seven days. It never controls execution.

Hermes upgrades require rechecking these boundaries. Missing Desktop capability
or session ownership leaves rows pending; uncertain admission requires diagnosis.
No second backend is created to steal an owned session, no Bot Chat substitution
is made, and the retired private completion queue is not restored.

## Development

```bash
hermes plugins doctor . --ci                 # manifest + registration contract
python3 scripts/run-tests.py --suite unit   # stdlib tests; no real Hermes host
(cd desktop && npm ci && npm test)          # isolated Desktop renderer tests
```

### What the green CI badge proves

The Python 3.11/3.12 matrix has two independent gates:

- `--suite unit`: standard-library unit and fixture tests, without a Hermes host.
- `--suite contract`: FastAPI/httpx API fixtures and prompt-toolkit CLI fixtures.
  Install their test-only dependencies with
  `python3 -m pip install -r tests/requirements-contract.txt`, then run
  `python3 scripts/run-tests.py --suite contract`.

Each Python module runs in its own process with a temporary `HERMES_HOME`.
Zero-test modules, skips, failures and uncaught worker-thread exceptions fail
these gates. Node 22 separately runs `npm ci && npm test` in `desktop`.
These checks use fake Pi processes and stubbed host boundaries; they do not
establish live Pi execution, production delivery or installed-host compatibility.
The plugin itself gains no runtime dependencies from the contract test suite.

Real-host acceptance is explicit and separate. It includes the actual plugin
loader, native CLI monitor and native post-turn renderer:

```bash
HERMES_AGENT_SOURCE=/path/to/hermes-agent \
HERMES_PYTHON=/path/to/hermes-agent/venv/bin/python3 \
/path/to/hermes-agent/venv/bin/python3 scripts/run-tests.py --suite host
```

Use a checked-out host revision with its dependencies installed and record its
commit alongside the result. The host gate fails when the source is unavailable
or tests are skipped. It loads the actual plugin manager and registry in a
**temporary home**, tests unknown-task dispatch and the outbox-to-host adapter;
only the leaf platform send is stubbed. It does not send a Telegram message,
launch real Pi/NInfer or validate the full production gateway. No installed
Hermes configuration or runtime code is modified by these tests.

For a manual channel check, see [the notification and continuation smoke prompt](docs/manual-channel-smoke.md).
The first passive progress notice is eligible after 90 seconds, later ones at least
180 seconds apart, and each requires observed progress. A brief task normally
produces only its terminal notice. The native CLI dock and Desktop card update
independently, about once a second. CLI, Desktop/TUI and messaging have distinct
host adapters; terminal continuation remains a separate path.

## License

MIT — see [LICENSE](LICENSE).
