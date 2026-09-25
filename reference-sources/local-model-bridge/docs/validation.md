# Validation

Package version: 0.1.1 (experimental).

On 2026-09-11, 21 fixture tests passed on macOS with Node.js 26.8.2. The Node 24/26
and Linux/macOS/Windows CI matrix is separate coverage and must be checked on GitHub.

The automated suite exercises observable transport and configuration behavior:

- Namespace aliases, collisions, historical calls and exact freeform tool input.
- Desktop notifications without a call id, with their tool trust level preserved.
- NInfer option translation and preservation of explicit reasoning effort.
- Reasoning history from another model: plaintext replay without duplication,
  omission of unreadable provider ciphertext and preservation of visible history.
- End-to-end loopback HTTP/SSE with local credential isolation and account/API routing.
- Rejection of unknown models, stale removed-local entries, local compaction and browser origins.
- Terminal failures for upstream errors and incomplete streams.
- UTF-8 fragmentation, CRLF, multiline events, gzip and zstd requests.
- Client cancellation closing the upstream connection.
- Catalog construction and configure/restore preserving unrelated user edits.

Live compatibility results are recorded separately from fixture tests. Successful
prototype use does not establish every platform, desktop build, model or long-task
combination for this refactored package.

The refactored package was also exercised with Codex 0.153.4, overriding configuration
only for isolated ephemeral client processes:

| Backend | Result |
|---|---|
| NInfer / Qwen 3.8 | Completed; returned the requested `BRIDGE_OK` marker |
| oMLX / Qwen 3.6 | Completed; returned the requested `OMLX_BRIDGE_OK` marker |
| OpenAI account model | Completed; returned the requested `CLOUD_BRIDGE_OK` marker |

Those initial isolated checks did not reconfigure the desktop installation.
Subsequent deployment testing used the actual desktop as described below.

Actual desktop deployment (build 26.903.71938 / Codex 0.153.4), 2026-09-11:

- oMLX completed a terminal `pwd` call and returned `OMLX_DESKTOP_BRIDGE_OK`
  through the deployed 0.1.0 package. The conversation contained about 85,000
  tokens and took 217 seconds, including memory-limited prefill on the model host.
- Switching that same conversation to NInfer exposed a 0.1.0 compatibility bug:
  historical reasoning summaries were rejected. Version 0.1.1 adds plaintext
  reasoning replay, covered by two regression tests.
- With the 0.1.1 candidate deployed, the same desktop conversation completed
  `pwd` and returned `WINDOWS_DESKTOP_BRIDGE_OK` in 20 seconds. The request retained
  its existing history and the selected model's reasoning effort.
- OpenAI account requests, including the active desktop conversation and the
  automatic approval-review model, continued through the bridge during deployment.

These are observed deployment checks, not a guarantee for every backend build,
conversation shape or sustained workload. The final packaging changes after the
candidate test affect documentation only.
