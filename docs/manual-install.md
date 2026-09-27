# Manual installation

These instructions reproduce the installer's setup using visible files and commands. They target Codex **0.157.1**, Python **3.11+**, and a Responses-capable local server. Run them on the computer containing the projects you want Codex to edit. The model server can run elsewhere.

## 1. Install Codex and obtain the adapter

With a supported Node.js LTS installation:

```bash
npm install -g @openai/codex@0.157.1
codex --version
git clone https://github.com/lolren/codex-local-bridge.git
cd codex-local-bridge
```

If you do not use npm, obtain the appropriate executable from the [official release for the tested version](https://github.com/openai/codex/releases/tag/rust-v0.157.1), place it on PATH as `codex`, and verify `codex --version`. See the [official CLI installation documentation](https://developers.openai.com/codex/cli) for platform choices.

## 2. Identify your served model

```bash
export LOCAL_LLM_URL='http://127.0.0.1:8000/v1'
curl -fsS "$LOCAL_LLM_URL/models"
export LOCAL_LLM_MODEL='YOUR_SERVED_MODEL_ID'
```

Replace the URL and ID. For an authenticated endpoint, add an Authorization header using an environment variable. The identifier in the API response is authoritative; a server may expose a short alias rather than a repository name.

## 3. Create a separate configuration

Choose a new directory. The following uses `~/.codex-local-manual` to avoid overwriting an installer-managed configuration:

```bash
export LOCAL_CODEX_HOME="$HOME/.codex-local-manual"
mkdir -p "$LOCAL_CODEX_HOME"
chmod 700 "$LOCAL_CODEX_HOME"
cp -n examples/config.toml "$LOCAL_CODEX_HOME/config.toml"
cp -n examples/local.config.toml "$LOCAL_CODEX_HOME/local.config.toml"
cp -n examples/models.json "$LOCAL_CODEX_HOME/models.json"
```

Edit these files before using them:

- In `config.toml`, set `model` to the exact served ID, set `model_catalog_json` to the **absolute path** of `models.json`, and set `model_context_window` to the server's configured context budget.
- In `local.config.toml`, set `model` to the same ID.
- In `models.json`, set `slug` to the same ID, adjust `display_name`, and set both context window fields consistently. Keep `"apply_patch_tool_type": "freeform"`.
- If you change the bridge port, update the provider's `base_url` as well.

Do not put a shell variable such as `$HOME` in the TOML path and expect shell expansion. Use an actual absolute path. The examples intentionally contain obvious placeholders.

For a server requiring a token, add this property inside `[model_providers.local-bridge]`:

```toml
env_key = "LOCAL_LLM_API_KEY"
```

Export that variable in the terminal used to launch Codex. Keep `requires_openai_auth = false`: your server's bearer token is separate from ChatGPT authentication.

Profiles in the tested CLI are separate files: `--profile local` reads `$CODEX_HOME/local.config.toml` on top of `config.toml`. Older tutorials using `[profiles.local]` can describe different CLI versions. The examples follow the tested version.

## 4. Run the adapter

In the repository directory, keep this process running in a terminal:

```bash
python3 bridge.py --upstream "$LOCAL_LLM_URL" --port 18081
```

The URL may include `/v1`; the bridge normalizes it. In a second terminal:

```bash
curl -fsS http://127.0.0.1:18081/health
curl -fsS http://127.0.0.1:18081/v1/models
```

Include an Authorization header for the second request if the upstream requires one. Health is local and does not check whether the model is loaded.

## 5. Start Codex with explicit local routing

```bash
export LOCAL_LLM_MODEL='YOUR_SERVED_MODEL_ID'
cd /path/to/your/project
CODEX_HOME="$HOME/.codex-local-manual" codex \
  --no-daemon \
  --profile local \
  --model "$LOCAL_LLM_MODEL" \
  -c 'model_provider="local-bridge"'
```

The profile, provider, catalog, and model ID must agree. `--model` alone does not select your local server. `--no-daemon` keeps this invocation on the explicitly selected configuration rather than involving a pre-existing daemon session.

If unrestricted execution is intentional, append `--yolo`. It disables approval prompts **and** the execution sandbox; it is not needed to enable native editing.

For a manual shell shortcut, put the full command above in your own script and append `"$@"`. The supplied installer provides a tested `codex-local` launcher with background startup, health checks, and separate settings, so it is usually simpler to use that once you understand the files.

## 6. Existing catalog or provider

To enable the catalog capability without replacing its other fields:

```bash
python3 scripts/enable_native_editing.py /absolute/path/models.json \
  --model YOUR_SERVED_MODEL_ID --dry-run
python3 scripts/enable_native_editing.py /absolute/path/models.json \
  --model YOUR_SERVED_MODEL_ID
```

The helper requires exactly one matching slug, backs up the file, and is idempotent. Route the provider through the adapter as well when your backend cannot consume custom tools. See the [patch bundle](../patches/README.md).

Start a **new** Codex session after changing capabilities. Test in a disposable project: ask for a small add/update/delete task and inspect the native file-change display. For automated verification, use the installer's launcher with [smoke_test.py](../scripts/smoke_test.py); it inspects actual JSON events as well as files.
