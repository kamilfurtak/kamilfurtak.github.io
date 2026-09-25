# Local Model Bridge

An experimental loopback proxy for using OpenAI account/API models and self-hosted
models from one Codex-compatible desktop model selector.

It changes user configuration, **not the desktop application's source, binaries,
signature or authentication files**. It is an independent community project,
not an official OpenAI, Ollama, oMLX or NInfer integration.

```text
Desktop / Codex client
        |
        v
Local Model Bridge (loopback)
        |-- OpenAI account or API endpoint
        |-- oMLX Responses endpoint
        `-- NInfer Responses endpoint
```

The bridge routes by explicit model ids, adapts tool namespaces and freeform tools,
and translates local Responses streams. Tools still execute on the client's task
host; selecting a remote model changes where inference runs.

## Status and requirements

- Node.js **24 or newer**, with no runtime npm dependencies.
- A desktop/Codex build that supports `openai_base_url` and `model_catalog_json`.
  Consult the [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference.md).
- A local server exposing **`/v1/responses`**. A Chat Completions-only server needs
  an additional adapter; an "OpenAI-compatible" label alone is not sufficient.
- Initial profiles: `omlx`, `ninfer`, and `native` Responses passthrough.
- The initial development environment used macOS and desktop build 26.903.71938 /
  Codex 0.153.4. Support for other desktop versions or client operating systems
  needs verification. A Windows inference server is separate from a Windows client.

The Python/Node prototype completed desktop requests and tool calls against oMLX
and NInfer. This package consolidates the transport into one Node process. See
[validation.md](docs/validation.md) for package-specific evidence and limitations.

## Setup from this repository

1. Copy the example next to the repository's `package.json`:

   ```sh
   cp examples/bridge.config.example.json bridge.config.json
   ```

   Edit each backend URL and `upstreamModel` to match your server. The `id` is the
   unique name the desktop will select. Keep only the models you use. Set context
   and reasoning options from the actual backend, not the example's conservative
   defaults. URLs can point to an existing SSH tunnel or lifecycle gateway.

2. Generate a catalog from **your own unmodified desktop catalog/cache**:

   ```sh
   node bin/bridge.cjs catalog --config bridge.config.json \
     --openai-catalog "$HOME/.codex/models_cache.json"
   ```

   The source must contain a `models` array with `slug` fields. If your build stores
   it elsewhere, supply that path. Start the desktop and let it load the model list
   first if the cache is absent. The command does not fetch models or read auth files.

   The generated file preserves the local client's schema and cloud model entries.
   It also records cloud provenance so removing a local route cannot accidentally
   send its stale catalog entry to OpenAI. Keep the generated file private; it may
   contain instructions supplied by your application. It is not part of releases.

3. Run the bridge and keep the terminal/process running:

   ```sh
   node bin/bridge.cjs serve --config bridge.config.json
   ```

4. In another terminal, install the two user settings:

   ```sh
   node bin/bridge.cjs configure --config bridge.config.json
   ```

   This backs up `config.toml` and prepends a marked block containing
   `openai_base_url` and `model_catalog_json`. It defaults to `$CODEX_HOME/config.toml`
   or `~/.codex/config.toml`. Use `--codex-config PATH` for another file. Existing
   provider/catalog overrides are left untouched and reported for review.

5. Restart the desktop, then select a configured local model in a local task.
   The CLI using the same configuration also uses the bridge. Cloud-only task
   environments and separately configured remote hosts are outside this setup.

The first version runs in the foreground. Use your existing service manager for
autostart if desired. The package does not install SSH tunnels, change Windows
Scheduled Tasks, wake hosts itself, or edit application bundles. Backend gateways
may implement wake-on-request behavior.

## Credentials and routing

OpenAI authentication remains managed by the client. Requests bearing
`chatgpt-account-id` use the account backend; other cloud requests use the OpenAI
API. These routes are deliberately fixed in the CLI. This project does not grant
account access or convert a subscription into an API entitlement.

Local requests discard incoming account tokens, account ids and cookies. If a
backend needs its own key, add `"apiKeyEnv": "MY_BACKEND_API_KEY"` to that model and
set the environment variable before starting the bridge. Never put keys in URLs.

The service only binds loopback and rejects browser-origin requests. `/health` and
`/v1/models` are local-only metadata operations and do not contact model servers.
Logs contain route, selected model, HTTP status and time; no prompts, tokens or
upstream error bodies. HTTP errors inside SSE are represented by `response.failed`;
missing terminal events never become fabricated successes.

## Known limitations

- This is version-sensitive integration. The account backend and desktop protocol
  can change independently of the documented configuration settings.
- Local built-in web search is omitted by the oMLX/NInfer profiles. Explicitly
  requesting that built-in tool fails. External callable tools remain available.
- Local server-side compaction is rejected; long sessions can therefore reach a
  context limit. It never falls back to cloud compaction for a local model.
- Local catalog entries advertise text only. Image/audio and every backend-specific
  structured-output feature are not validated. NInfer text-format options fail
  explicitly rather than silently dropping a requested schema.
- NInfer's unsupported reasoning-summary and event-padding options are normalized;
  the requested reasoning effort is preserved. See [compatibility.md](docs/compatibility.md).
- Automatic approval reviews can still use a cloud model if the client requests
  one in the cloud catalog. Local inference does not make the entire app offline.
- HTTP/SSE is used. WebSocket upgrade requests receive 426 so compatible clients
  can fall back to HTTP.
- Catalogs are snapshots. Regenerate from the current original app cache using
  `catalog ... --force`, then restart the bridge and desktop after model changes.
- Backend cancellation is best effort: the bridge closes its connection, but a
  backend's already-dispatched wake/start operation may continue.

## Undo

```sh
node bin/bridge.cjs restore
```

Use the same `--codex-config` path if supplied during setup. Restore removes only
the exact managed block and preserves subsequent unrelated edits. If someone
changed that block, it reports the conflict rather than overwriting their changes.
The original backup is retained. Restart the desktop before stopping the bridge.

## Tests and packaging

```sh
npm test
npm pack --dry-run
npm pack
```

Tests use temporary configuration and loopback fixtures. They do not call OpenAI,
load a model, or wake a machine. The npm package includes an explicit file allowlist;
runtime configuration, generated catalogs and captured conversation data are not
included. A tarball can be installed with `npm install -g ./local-model-bridge-0.1.1.tgz`.

MIT-licensed project code. Application catalogs, model weights and external engines
are not redistributed by this project.
