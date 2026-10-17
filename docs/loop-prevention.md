# Reducing text and tool loops

Sampling defaults are the first step. A client that sends no temperature can
otherwise get greedy decoding from Strata. Orca's model card recommends
temperature 1.0, top-p 0.95, top-k 20 and min-p 0 for thinking replies.

Sources:

- [Orca model card](https://huggingface.co/orcarouter/Qwen3.8-Flash-Next-Uncensored-GGUF)
- [Qwen's thinking and non-thinking defaults](https://huggingface.co/Qwen/Qwen3.8-Flash-Next#best-practices)
- [Strata's diagnosis](https://github.com/Niko1221/Strata/issues/980)
- [Replay tests of real tool loops](https://github.com/Niko1221/Strata/issues/1560)
- [Strata's reasoning budget and recovery](https://github.com/Niko1221/Strata/blob/main/docs/DETAILS.md)

## Strata run configuration

Put these keys outside `args` in the model's run JSON:

```json
{
  "sampling": {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "repetition_penalty": 1.0
  },
  "reasoning_budget_tokens": 16000,
  "reasoning_loop_recovery": "recover",
  "reasoning_close_retry": true,
  "repeat_stop_tokens": 256
}
```

The budget caps thinking per response; it does not lower the context window.
Requests can override it, including with 0 to remove the cap. Hard reasoning
tasks can benefit from a larger budget. Recovery runs once, requires the native
high-effort instruction, and skips image requests; the budget still applies to
those requests. It does not detect tool loops spanning multiple responses.

The included [Strata patch](../patches/strata-nonthinking-sampling.patch) adds an
optional `sampling_non_thinking` block to the Python server. This is a local
extension tested against checkout `fb58e0d` (engine 0.1.41), not an upstream
configuration option. Apply it from the Strata checkout and restart its server:

```bash
git apply /path/to/codex-local-bridge/patches/strata-nonthinking-sampling.patch
python3 -m unittest serve.test_sampling_modes
```

With that patch, add:

```json
"sampling_non_thinking": {
  "temperature": 0.7,
  "top_p": 0.8,
  "presence_penalty": 1.5
}
```

This overlays `sampling` only when thinking is disabled. Shared Strata Chat
settings and explicit client parameters still win. Without the patch, send
these values in each non-thinking request instead.

## Guarding repeated tool calls

Enable the adapter's optional guard during installation:

```bash
python3 install.py --upstream http://127.0.0.1:8080/v1 \
  --model gpu_server_local --context-window 262144 --loop-guard
```

Repeat your other installation options and add `--force` when updating an
existing installation. For a manual bridge, append `--loop-guard`. Existing
installed launchers also accept `"loop_guard": true` in `installation.json`;
restart that bridge after changing it.

The guard compares tool names, namespaces, normalized JSON arguments and
results since the latest user message. It detects one repeated call or a
cycle of up to four calls. Execution timings and chunk IDs are ignored;
actual output and exit status are retained.

- After four identical completed cycles, the next request gets a repetition
  penalty of 1.1 over 512 tokens, unless the client explicitly supplies those
  settings. Prompt and history bytes are not rewritten.
- After six identical completed cycles, the bridge rejects the next request
  with HTTP 400 and `error.code = "local_tool_loop"`, before calling the model.
  This ends the runaway turn instead of endlessly retrying it.
- A new user instruction or a changed call/result resets the trailing streak.
  Process polling, waits, user-input requests and image viewing are exempt.

This limits damage; it does not prove that an agent has solved the task.
Intentional unchanged repeated operations may also trigger it. Send a new
instruction to continue with another approach, or disable the guard and
restart the bridge if deliberate repetitions are part of your workload.
Sampling can reduce speculative acceptance and speed; measure useful task
completion rather than tokens per second alone.

## Codex thinking choices

For a server that supports all four modes, set these fields in its model
catalog entry:

```json
"default_reasoning_level": "medium",
"supported_reasoning_levels": [
  {"effort": "none", "description": "Answer without internal reasoning."},
  {"effort": "low", "description": "Short reasoning for straightforward tasks."},
  {"effort": "medium", "description": "Balanced reasoning for coding and debugging."},
  {"effort": "high", "description": "More thorough reasoning for difficult tasks."}
]
```

Set `model_reasoning_effort = "medium"` in the launcher's selected
`local.config.toml`. Keep the served model ID, provider and context window
consistent with the running server. Reopen Codex to reload the catalog.
For a one-session override:

```bash
codex-local --yolo -c 'model_reasoning_effort="high"'
```

## Loading a second RAM-backed instance

On this server, the second RTX 3090 instance timed out at Strata's default
900-second startup deadline while preparing the expert arena. The Linux
transparent-huge-page mode and defragmentation policy were both `madvise`.
Strata's `src/core/pinned.cu` describes expensive compaction in this case.

The dashboard's Docker launcher now passes:

```bash
-e STRATA_NO_ARENA_THP=1 -e STRATA_ENGINE_READY_S=1800
```

Skipping the arena's huge-page request let the second instance become ready
in about a minute with the first instance still running. The entire expert
arena remains registered with CUDA; this does not lower the context window
or cap the RAM expert tier. These environment settings apply to both profiles
on subsequent starts. The larger timeout also allows slower cold starts.

## Installed validation (2026-10-11)

Both `codex-local` (8080) and `codex-local1` (8081) passed live native file
add/update/delete checks and image reading through Codex 0.162.1. All four
thinking modes were exercised against both native APIs: None produced no
reasoning content, while Low, Medium and High did. The actual Codex model
catalog advertised all four modes and text/image input for both installations.

The bridge and focused Strata regression suites passed 79 tests. A controlled
upstream that repeated an unchanged failing command was also exercised through
the real Codex CLI: the soft penalty appeared after four completed calls, and
the seventh generation request was rejected after six calls. Codex ended the
turn with `local_tool_loop` instead of retrying forever.

Both running APIs report a 262144-token context, and both runtime configs
retain the 128-image limit. These checks used short requests and one image;
they did not fill the entire context or send 128 images in one request.
