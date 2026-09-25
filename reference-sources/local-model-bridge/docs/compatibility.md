# Protocol profiles

All profiles expect a Responses endpoint at `<baseUrl>/responses`.

| Feature | `native` | `omlx` | `ninfer` |
|---|---|---|---|
| Request body | Preserved except configured model id | Tool compatibility | Tool and option compatibility |
| Namespaced tool definitions/history | Backend handles them | Flattened and restored | Flattened and restored |
| Long tool names | Backend handles them | Deterministic alias, at most 64 characters | Same |
| Freeform custom tools | Backend handles them | JSON wrapper, exact input restored | Same |
| Desktop tool notifications without call ids | Backend handles them | Completed call/output pairs | Same |
| Built-in web search | Backend responsibility | Omitted by default | Omitted by default |
| Reasoning effort | Preserved | Preserved | Preserved |
| `reasoning.summary` | Preserved | Preserved | Omitted |
| Reasoning history after a model switch | Preserved | Preserved | Plaintext replay; provider ciphertext omitted |
| `stream_options` | Preserved | Preserved | Known desktop options normalized to `include_obfuscation: false` |
| `parallel_tool_calls` | Preserved | Preserved | Omitted; engine controls execution |
| Encrypted reasoning include, cache metadata | Preserved | Preserved | Known unsupported options omitted |
| Requested text schema | Backend responsibility | Backend responsibility | Explicit error |

Choose `native` only when the backend already understands the desktop's complete
Responses dialect. The label is not a claim that every local server supports it.
Local catalog entries conservatively disable search and image capabilities.

Tool aliases are deterministic and reversible for the current tool catalog. Tool
history uses the same encoding even if an old tool is no longer offered. Alias
collisions are rejected. Notifications remain tool results, not promoted user or
system instructions; models can differ in how they interpret those notifications.

Freeform tool argument deltas are buffered until the JSON wrapper is complete;
partial JSON is not exposed as raw patch text. Completed calls retain their
original namespaces, names and input text. The bridge never executes tools.

When switching an existing conversation to NInfer, historical reasoning summaries
cannot be sent in their original format. The adapter replays existing raw reasoning
text when available; otherwise it reuses the summary text as assistant reasoning
text. A summary remains a summary in substance: this does not recover omitted
reasoning. It does not promote that text to user or system instructions. Encrypted
provider reasoning is not portable and is omitted; an item containing only opaque
reasoning is removed while visible messages and tool history remain intact.

The SSE parser handles UTF-8 split across chunks, LF/CRLF frames and multiline
data fields. `response.completed`, `response.failed` and `response.incomplete` are
terminal events. An EOF without any terminal event produces a failure. Unknown
request options with no safe NInfer translation are rejected.

Each deployment supplies model IDs, URLs, context sizes and supported reasoning
levels. There is no built-in host inventory or Windows lifecycle policy.
