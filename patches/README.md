# Complete fix and configuration patches

This fix does not modify the Codex binary or the vLLM source tree. Its components are:

1. **Model capability:** enable `apply_patch_tool_type = freeform` in Codex's custom model catalog.
2. **Transport adapter:** [bridge.py](../bridge.py) translates custom tool definitions, calls, results, history, and SSE events to/from the server's function format.
3. **Provider routing:** point Codex at the adapter, use the Responses transport, and disable OpenAI-account authentication for this custom provider.
4. **Reliable invocation:** [launcher.py](../launcher.py) explicitly selects the separate configuration, provider, profile, and model and starts the adapter.

The [installer](../install.py) installs all four. The [manual guide](../docs/manual-install.md) explains each file.

## Patch an existing catalog safely

```bash
python3 scripts/enable_native_editing.py /absolute/path/models.json \
  --model YOUR_SERVED_MODEL_ID --dry-run
python3 scripts/enable_native_editing.py /absolute/path/models.json \
  --model YOUR_SERVED_MODEL_ID
```

The helper changes only the matching model entry, requires exactly one match, preserves other fields, and creates a timestamped backup. Start a fresh Codex session afterward. Enabling the catalog alone does not fix a server that drops custom tools.

## Reviewable diffs

- [native-editing.patch](native-editing.patch) shows the capability change against the example catalog without that field.
- [provider-routing.patch](provider-routing.patch) shows the URL change from a direct local server to the loopback adapter, assuming the other example provider settings already exist.

These are conventional unified diffs for review or `git apply` against matching preimages. They are **not** Codex native `*** Begin Patch` inputs. They use placeholder configuration, not private machine paths. Your catalog/provider may differ, so prefer the targeted helper and manual guide over blindly applying the diffs.

For the pinned CLI, `"function"` is not the supported catalog value for `apply_patch_tool_type`. Codex receives its native freeform tool; only the upstream server sees the temporary function representation.
