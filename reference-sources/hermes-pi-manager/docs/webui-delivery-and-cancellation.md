# WebUI delivery and startup cancellation

## Host routing

`ui_session_id` is not a Desktop discriminator. WebUI records also contain it.
`webui_host.py` resolves only modules already loaded in the running WebUI, the
exact persisted UI session and its matching profile home. It does not import a
second host, call an HTTP endpoint, fall back to another platform, or alter
model/provider/reasoning configuration. Old outbox rows mistakenly projected as
`tui` are routed using the owning task's original WebUI origin.

Passive terminal notices use WebUI's existing `bg_task_complete` SSE renderer.
They invoke no model. An absent subscriber defers delivery without consuming
retries. The outbox's `sent` means accepted by a live subscriber queue, not proof
that a browser rendered it. Browser event-id deduplication remains best effort.
The current host has no passive Pi progress/stall renderer: those notices are
explicitly marked undeliverable instead of being mislabeled as completion.
Desktop's progress notices and inline activity card remain unchanged. WebUI is
no longer offered the unsupported Desktop `::pi-live` directive.

Terminal continuation is a separate operation using native `start_session_turn`.
The `process_wakeup` source preserves the host's credential-pause and model
selection behavior. The existing plugin injection grant is checked in the
saved target profile. Native busy/stale-runtime responses defer without spending
the retry budget. An ambiguous exception becomes `uncertain`, not automatic
replay. The existing durable wake lease/CAS prevents repeated accepted delivery.
No exactly-once end-to-end guarantee is claimed. Acceptance does not prove the
resumed model turn completed; inspect that session's persisted stream/response.

## Startup cancellation

The per-task admission lock serializes first prompt admission, transport
publication and abort cleanup. Spawning itself remains outside that lock, so an
abort can be requested while the OS spawn is delayed. A second task is not
serialized behind the first.

`boot_pending` retains the execution owner until the boot thread has accounted
for a late process. `cancellation_pending: true` is not `ABORTED`: the process
may not yet be observable, or cleanup may lack an exit confirmation. A late
process receives cancellation, never its task prompt. Abort escalates through
RPC, terminate and kill; failed cleanup retains ownership and its diagnostic.
An explicit retry can reconcile cleanup. Manual cancellation never requests a
terminal wake. A final state cannot be overwritten by an ordinary stale state
update; only explicit recovery has the reopening path.

An abort from a foreign process with no attached transport records the request
but cannot claim cleanup. This change does not implement a cross-host kill
service, automatic model failover or automatic merging of concurrent writers.
Use separate worktrees and bounded scopes for concurrent writing tasks.

## Checks and rollout

Run the repository's isolated module regression, including
`test_start_abort`, `test_webui_delivery`, `test_parallel_tasks`, Desktop/CLI
routing, recovery and tools/loader coverage. The recovery notification test
waits for the actual outbox row, not just the earlier SETTLED transition.

The implementation has also been exercised with a real Pi process aborted while
spawn was gated: no prompt was sent, process exit was observed, ownership was
released, and no wake was requested. An isolated contract check against installed
WebUI source exercised the native entrypoint and SSE channel; its downstream
model launch was a controlled fake, not browser acceptance.

Live browser acceptance (2026-09-20, WebUI conversation `755e481442c5`, task
`pi-915a8a3cb9a6`): one bounded real task dispatched from the browser, the
settled notice was delivered on the first attempt, and the continuation wake
started the resumed turn in the same conversation. That run also exposed the
native admission response shape in direct mode
(`api/routes.start_session_turn` → `_start_chat_stream_for_session`): a
success carries `stream_id` and NO `_status` key at all (`_status` exists only
on error paths and in the journal-adapter wrapper). The earlier classifier
required `_status == 200`, so a successful admission was recorded `uncertain`
while the wake turn actually ran. Admission is now classified by the stream id
(`_status` absent or 200), ambiguous responses stay `uncertain`, and a
regression test pins the real shape (red on the previous code).

Before activation, review the source diff and active plugin path, preserve the
rollback commit, inspect pending origins and obtain approval for a backend
restart. Do not restart the WebUI from its own active turn. After activation:

1. Confirm the loaded plugin revision and unchanged executor configuration.
2. Run one bounded real task from the intended WebUI session.
3. Verify exact task state, notification row and one accepted wake.
4. Verify the actual resumed session response and no duplicate launch.
5. Check that busy-session deferral and a second independent task still work.

Do not report production completion from local tests or a file deployment alone.
