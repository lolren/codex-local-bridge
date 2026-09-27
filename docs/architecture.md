# Why a local model could not use native editing

There were two independent failures in the tested setup. Fixing only one was insufficient.

## 1. Codex did not register the tool for the custom model

Codex selects capabilities from its model metadata. The custom catalog lacked:

```json
"apply_patch_tool_type": "freeform"
```

At the tested `rust-v0.157.1` release, the protocol's `ApplyPatchToolType` enum has the `Freeform` variant. The tool plan registers the patch handler when the corresponding model capability is present. Older advice to set this property to `"function"` does not match this release.

Relevant upstream source:

- [Model metadata and ApplyPatchToolType](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/protocol/src/openai_models.rs)
- [Tool registration](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/core/src/tools/spec_plan.rs)
- [Native patch specification](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/core/src/tools/handlers/apply_patch_spec.rs)
- [Native patch handler](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/core/src/tools/handlers/apply_patch.rs)

The catalog entry must match the requested model's exact slug, and `model_catalog_json` must point to that catalog. The installer generates both. The instructions template also encourages native editing, but instructions alone cannot register a missing tool.

## 2. The local server's template path omitted custom tools

The inspected vLLM Responses-to-template path selected tools of type `function`. Codex advertises `apply_patch` as a custom tool with a freeform grammar, so it was absent from the model's usable tools even after fixing the catalog. This observation applies to the tested server build; newer backends may differ.

The adapter exposes the patch tool upstream as a standard function:

```json
{
  "type": "function",
  "name": "apply_patch",
  "parameters": {
    "type": "object",
    "properties": {"input": {"type": "string"}},
    "required": ["input"],
    "additionalProperties": false
  },
  "strict": true
}
```

The model supplies `{"input":"*** Begin Patch\n...\n*** End Patch"}`. The bridge unwraps that string and restores a **native custom tool call** for Codex. Codex's own patch handler applies it and emits native file-change events. The bridge never executes tools or writes project files.

## Round trips and streaming

| Direction | Translation |
| --- | --- |
| Outbound tool definitions | Custom tool → function with one string field |
| Outbound history | `custom_tool_call` → `function_call`; input becomes JSON arguments |
| Outbound tool results | `custom_tool_call_output` → `function_call_output` |
| Outbound explicit tool choice | Custom tool choice → function tool choice |
| Incoming result items | Converted function call → original custom tool call |
| Incoming SSE arguments | Accumulate escaped JSON, decode it, emit custom input delta/done |
| Incoming completed response | Convert output items consistently with the streaming events |

Call IDs, ordinary function tools, and messages are retained. The SSE bridge renumbers events after introducing or removing events. A patch is emitted once its argument JSON is complete; it is not displayed token by token. The parser understands multiline SSE frames, CRLF, and `[DONE]`.

Codex's event consumer is in [the Responses SSE implementation](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/codex-api/src/sse/responses.rs). Unit tests cover escaped Unicode, partial argument chunks, history continuation, and an upstream that emits a completed item without argument delta events.

Malformed function arguments fail with a bridge error instead of being treated as patch text. Custom grammars are not enforced during upstream generation; Codex validates the returned patch. Other custom tool names are translated generically, but their semantic compatibility is unverified. Namespace structure is preserved; this is not a namespace-flattening adapter.

## Network and execution boundary

The bridge binds to `127.0.0.1` only and forwards to one configured HTTP(S) upstream. It passes the client's Authorization header to that upstream, rejects redirects, and ignores environment HTTP proxy variables. Use the final destination URL or an explicit trusted reverse proxy. HTTPS uses Python's normal certificate verification.

The `/health` response contains the adapter version, process ID, and installation instance ID. The launcher checks the instance ID before reusing or stopping a process. Adapter request logs omit bodies, Authorization headers, and query strings. The model server and Codex have their own logging policies.

The bridge is not an authentication gateway or a public multiuser service. A local process that can reach the listener can invoke the upstream using its own supplied credentials. Codex's configured sandbox and approval policy remain responsible for execution permissions.

The adapter does **not** translate Chat Completions to Responses, implement WebSockets, provide hosted tools, load models, configure GPUs, or guarantee that a model chooses appropriate tools. The generated provider disables WebSockets so Codex uses HTTP/SSE.
