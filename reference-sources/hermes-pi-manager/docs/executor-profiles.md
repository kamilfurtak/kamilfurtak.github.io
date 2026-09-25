# Approved executor profiles

Pi Manager can select an operator-approved profile for each task. This is an
additive capability, not an automatic fallback router and not a change to the
main Hermes model. Credentials remain in Pi's native provider authentication.

## Configuration

In the active Hermes home, create `policy/pi-executors.json` only after testing
and authorizing the exact provider/model/thinking choices. The operator owns
this file; the model-facing tool only accepts a profile name.

```json
{
  "default": "local",
  "profiles": {
    "local": {
      "provider": "ninfer",
      "model": "qwen3.8-27b",
      "thinking": "medium"
    }
  }
}
```

This example preserves the historical local selection. It does not authorize a
live deployment. Add a commercial profile only with the exact model supported by
Pi and the account. `pi --offline --list-models` lists authenticated models, not
the complete catalog. No result may mean missing authentication. Use Pi's native
`/login`; do not copy Hermes/Codex OAuth tokens. A thinking level accepted by the
CLI is not proof the model supports it: Pi can clamp unsupported levels.

The policy is loaded on manager initialization. Reload/restart needs the host's
normal authorization and lifecycle procedure; editing the file is not proof an
existing manager adopted it. Missing file retains constructor defaults. Invalid
JSON, unknown keys/profiles or malformed choices fail closed.

## Dispatch and recovery

`pi_task(prompt=..., cwd=..., executor="local", verifier_argv=[...])` selects an
approved profile. Omission selects the configured default. With no profile file,
omission preserves legacy behavior and explicit profile names are rejected.

Before spawn, each new task records the profile name and an exact snapshot of
provider/model/thinking in additive SQLite columns. Start results, `pi_status`
and `pi_digest` expose that requested selection. Profile-mode startup checks Pi's
actual `get_state.model.provider`, `model.id`, and `thinkingLevel` before sending
the prompt. Missing/mismatched state rejects startup and terminates the process.
`executor_identity_checked` records whether this check was applicable; legacy
unprofiled tasks retain the older RPC compatibility contract.

Recovery uses the stored snapshot, not today's profile definition or manager
default. It checks runtime identity again before replay. Removing a profile does
not rewrite historical snapshots. To revoke an in-flight task, explicitly abort
it with the established lifecycle controls; policy editing is not revocation.
A corrupt snapshot is rejected. Old tasks without a snapshot retain legacy
recovery only while profile mode is absent; profile-mode recovery refuses to
invent historical model identity. Terminal/verifier-only recovery is unchanged.
The snapshot is execution metadata, not an immutable security audit log.

## Safe replacement is a separate decision

There is no implicit fallback after quota, authentication or process failure.
A replacement must use a new task ID after the prior writer is proven terminal,
partial changes have been reviewed, and a new bounded packet specifies the
remaining work. Do not resend the original patch blindly. Recovery of the same
task must never change its model. An authentication error is not quota exhaustion.

One writer per checkout; concurrent tasks need separate worktrees and actual
backend capacity. The manager's per-task OS lock does not enforce checkout-wide
exclusion. Main-session authorship and final review remain unchanged.

## Rollout gates

Do not switch production policy merely because fixture tests pass. Require:

- regression suite and source/diff review;
- real local and commercial tasks, independent verifiers and recorded model state;
- exact requested reasoning, not silently clamped reasoning;
- safe start/abort/replacement (known prerequisite: repository issue #1, section A);
- passive delivery and continuation in the actual target host, separately from
  execution and verification success;
- no automatic replay of accepted/uncertain delivery after reload.

The existing CLI/Desktop lifecycle issues are not fixed by this feature. Keep
unrelated host repairs and deployment acceptance explicit. No native Hermes
subagent settings or main-model fallback settings are modified by this module.
