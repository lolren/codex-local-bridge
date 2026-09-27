# Connecting your model server

The workstation runs Codex and the adapter. The server runs inference. The server receives prompts and tool definitions/results; Codex executes tools and edits files on the workstation.

## Required API behavior

| Feature | Required behavior |
| --- | --- |
| Model discovery | `GET /v1/models` returns the model's exact served ID |
| Generation | `POST /v1/responses` accepts messages, function definitions, and prior tool results |
| Streaming | SSE Responses events with function-call items, arguments, and completion events |
| Tool calling | Server parser and model template produce valid function calls, not just textual descriptions |
| History | Function calls and their results remain usable across turns |

First verify model discovery directly:

```bash
curl -fsS http://127.0.0.1:8000/v1/models
```

Model discovery alone is insufficient. The smoke test checks an actual multi-step tool session. A successful chat response from `/v1/chat/completions` does not establish Responses compatibility.

## vLLM

Use a vLLM build that supports your model, Responses, and its tool parser. The [vLLM Codex guide](https://docs.vllm.ai/en/latest/serving/integrations/codex/) documents the Responses integration and model-specific parser flags. The [tool-calling documentation](https://docs.vllm.ai/en/latest/features/tool_calling/) explains parser selection.

This is a **template**, not a tested launch recipe for every model or GPU:

```bash
export MODEL_PATH='YOUR_MODEL_PATH_OR_REPOSITORY'
export TOOL_PARSER='YOUR_MODEL_SPECIFIC_TOOL_PARSER'

vllm serve "$MODEL_PATH" \
  --host 127.0.0.1 --port 8000 \
  --served-model-name local-coder \
  --max-model-len 32768 \
  --enable-auto-tool-choice \
  --tool-call-parser "$TOOL_PARSER"
```

Add tensor parallelism, quantization, reasoning-parser, and memory options from the **specific model's serving instructions**. There is no universal parser or GPU count. Install this client using `--model local-coder` if you used that serving alias.

The release's live test used the specialized [Qwen3.8 Flash Next W4A16 FP8PLE deployment](https://huggingface.co/albucino/Qwen3.8-Flash-Next-W4A16-FP8PLE), with its required backend modifications already installed. This repository packages the Codex integration; it does not reproduce that model's GPU offloading, expert-cache, or MTP setup. Follow the model maintainer's deployment instructions for those components.

## Remote GPU server

Pass the server's reachable address to the installer:

```bash
python3 install.py \
  --upstream http://gpu-server.example:8000/v1 \
  --model local-coder \
  --context-window 32768
```

`gpu-server.example` is a placeholder. The adapter still listens only on the workstation's loopback interface. If you use an SSH tunnel, point `--upstream` at the local end of the tunnel instead. Make sure your server's binding and firewall permit your chosen connection.

For a remote authenticated HTTPS server:

```bash
python3 install.py \
  --upstream https://your-server.example/v1 \
  --model local-coder \
  --api-key-env LOCAL_LLM_API_KEY
```

Set `LOCAL_LLM_API_KEY` through your normal secret-management mechanism in the terminal used for Codex. The installer stores only its variable name. The bridge receives the bearer header from Codex and forwards it to the configured server. It does not follow redirects or use `HTTP_PROXY`/`HTTPS_PROXY`; configure the final upstream URL.

## Context and reasoning

Match `--context-window` to the running server's actual limit, not just the model card. A 262,144-token context requires server support and sufficient KV-cache capacity; setting that number in Codex does not supply either. The release smoke test used that configured limit but did not fill the context with 262K tokens.

The installer omits an explicit reasoning effort by default. If your backend requires or supports a specific value, supply it with `--reasoning-effort`. If requests fail with an unsupported effort, remove that option and reinstall with `--force`, or edit `local.config.toml` and the catalog consistently.

## Other servers and tools

- **Other Responses servers:** expected to work only if they satisfy the table above; run the smoke test. This project does not claim broad model certification.
- **Chat Completions-only servers:** need a separate, compatible Responses implementation. This adapter does not supply one.
- **Ollama and LM Studio:** Codex has its own `--oss --local-provider` route for those providers. That is a separate setup; do not mix it with this custom provider. Check [official configuration documentation](https://developers.openai.com/codex/config-advanced) for your CLI version.
- **MCP tools:** configure them in this installation's isolated Codex home, if needed. Existing `~/.codex` MCP configuration is not automatically copied. This adapter leaves ordinary function tools intact, but the local model still needs to understand their schemas.
- **Hosted tools:** the adapter does not provide web-search services, image generation, or computer-use services. Model capabilities and external integrations must be supplied separately.

If a backend already handles Codex custom tools correctly, you may be able to use it directly with a correct model catalog. Test that path independently; the bridge is for the function/custom-tool mismatch described in [architecture](architecture.md).
