# Troubleshooting

Start with these commands from the repository:

```bash
codex-local --bridge-status
python3 scripts/doctor.py --start-bridge
python3 scripts/smoke_test.py --save-log /tmp/codex-local-smoke.jsonl
```

For a custom installation add `--prefix /your/install` to `doctor.py` and `--launcher /your/install/bin/codex-local` to the smoke test. Saved session logs can contain paths, prompts, and tool output; inspect and redact before sharing.

## “Model is not supported when using Codex with a ChatGPT account”

The invocation is selecting a cloud provider or an existing cloud-configured session. `--model some-local-id` alone does not change the provider. Use the installed `codex-local` launcher and start a fresh session. It sets the isolated `CODEX_HOME`, explicit profile, model, and provider, with `--no-daemon`.

For a manual setup, verify `model_provider`, the corresponding `[model_providers.…]` section, `wire_api = "responses"`, and `requires_openai_auth = false`. Check the exact command in [manual installation](manual-install.md). A local server bearer token belongs in `env_key`; it is not a ChatGPT login.

## No native edit tool, or the model only writes shell commands

Both halves of the fix are required for the affected backend:

1. The matching model catalog entry contains `"apply_patch_tool_type": "freeform"` and Codex loads that catalog.
2. The provider URL points to the bridge, which forwards to the actual server.

Start a new session after changes. Confirm the configured model ID matches `/v1/models`. Check the server's tool parser and chat template. The bridge makes the tool available but cannot force every model to choose it correctly. The smoke test distinguishes native `file_change` events from shell-based writes.

Do not set `apply_patch_tool_type` to `"function"` for the pinned CLI. The backend sees a function representation; Codex still expects its native freeform tool.

## HTTP 404, 400, or 401

| Symptom | Check |
| --- | --- |
| `/v1/responses` returns 404 | Server lacks Responses support, or the URL path is wrong |
| Model not found | Use the exact served ID, including any alias |
| Unsupported parameter / reasoning effort | Match the server's supported settings; reasoning effort is optional |
| Tool schema rejected | Verify server function-tool support and model parser; namespace support varies |
| 401 | Export the environment variable named by `--api-key-env` with the server's actual token |
| Bridge 502 | Check server availability or malformed tool arguments; inspect backend logs |
| Redirect response | Use the final URL directly; the bridge intentionally does not follow redirects |

Use the direct upstream `/v1/models` URL to separate server reachability from bridge configuration. The adapter has a 600-second upstream read timeout. A server that never completes a tool call may need parser or generation-setting changes.

## A different adapter occupies the port

Each installation has an instance ID. The launcher refuses to reuse a different instance so it cannot silently send requests to the wrong model server. Stop the intended existing installation using its own `--bridge-stop`, or choose a different `--port` and, for another installation, a different `--prefix`.

Do not kill an arbitrary PID based on the port number. A manually launched adapter has instance ID `manual` and will not be adopted by an installer-managed launcher.

## `codex-local: command not found`

Use the full path printed by the installer, or add the launcher directory to your shell PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Persist that line in the appropriate startup file for your shell if needed. For `--prefix`, use the prefix's `bin` directory instead. A launcher generated on one machine is not installed on another machine automatically.

## Sandbox or bubblewrap fails before the tool runs

An error such as `bwrap: RTM_NEWADDR: Operation not permitted` is a host sandbox/network-namespace issue, separate from the model protocol. Check the host/container's support for the sandbox required by your Codex version. The bridge cannot fix it.

If you deliberately accept unrestricted execution, `codex-local --yolo` bypasses the sandbox and approval prompts. The live release test used this option in a temporary project because of host sandbox restrictions. That does not make the temporary directory a security boundary.

## Unexpected patch results

Codex's patch syntax is not a Git unified diff. Use `*** Begin Patch`, `*** Update File`, `@@`, and `*** End Patch`. Native edits can normalize line endings and overwrite paths; see [native patch behavior](native-editing.md) for the maintainer's probes and source-checked limitations.

## Logs and services

For automatic background startup, adapter logs are in `~/.local/share/codex-local-bridge/bridge.log`. For systemd:

```bash
systemctl --user status codex-local-bridge.service
journalctl --user -u codex-local-bridge.service -n 80 --no-pager
```

These adapter logs omit request bodies and credentials. Codex's own session logs and upstream logs have separate behavior. The `doctor` command checks configuration and model discovery; only the smoke test exercises generation and real native file changes.
