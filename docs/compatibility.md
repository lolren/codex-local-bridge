# Compatibility and validation

## Verified baseline

| Component | Validation |
| --- | --- |
| Codex CLI | `0.157.1`; source tag `rust-v0.157.1` |
| Original workstation | Linux, Python 3.12.3 |
| Portable release validation host | Linux, Python 3.14.4 |
| Backend | Customized vLLM deployment; package reports `0.1.dev20073+g8e685d198` |
| Model | `albucino/Qwen3.8-Flash-Next-W4A16-FP8PLE` |
| Configured server/client context | 262,144 tokens |
| Release live execution policy | Explicit `--yolo`, due to host sandbox limitations |
| Python dependencies | Standard library only; installer requires 3.11+ |

The customized server's deployment is model-specific, so there is no claim that an arbitrary stock vLLM release serves these weights. Follow the [model deployment instructions](https://huggingface.co/albucino/Qwen3.8-Flash-Next-W4A16-FP8PLE). No private IP addresses, local account credentials, model files, or raw session logs are included here.

## Live release check

On 2026-09-27, the portable installer created an independent installation under a temporary prefix and used an alternate loopback port. The diagnostic command confirmed the model ID and context limit. A real Codex session received an ordinary task prompt, without naming the patch tool:

- Correct the addition function in an existing file.
- Create a README.
- Delete an obsolete file.
- Preserve and run three existing unit tests.

The resulting session contained completed native `file_change` events for **add, update, and delete**. The verifier independently checked the resulting files, hashed the original test file to confirm it was unchanged, and reran all three tests successfully. This demonstrates the native edit round trip rather than merely accepting the model's claim of success.

Both an installation with an explicit `medium` reasoning effort and a second installation with the default omitted effort passed this check. The second installation also exercised the backup-and-replace upgrade path while stopping and restarting its adapter.

The configured context was 262,144, but this short smoke task was **not a full-context load or performance benchmark**. It also does not establish model quality on large projects or the behavior of hosted tools.

## Automated tests and CI

Run:

```bash
python3 -m unittest discover -v
```

The 24 tests cover request/history conversion, ordinary-tool preservation, streamed argument reconstruction, completed responses, malformed arguments, real HTTP forwarding, bearer-header forwarding, redirects, invalid requests, installation, safe overwrite refusal and backups, automatic startup without reverse-DNS resolution, provider/environment selection, instance identity, and the targeted catalog patcher. The two published unified diffs were also applied to their example preimages and verified to reproduce the documented configuration.

The [CI workflow](../.github/workflows/tests.yml) runs these offline tests on Linux with Python 3.11–3.14 and macOS with Python 3.12. CI uses a mock upstream and fake Codex executable; it does not download model weights, access the maintainer's server, or claim a live macOS/WSL model test. Consult the workflow's actual result for the current commit.

See [native-editing.md](native-editing.md) for the separate user-supplied operation report and its limits.

## Compatibility boundaries

- Newer or older Codex versions need verification; model metadata and profile syntax can change.
- Other Responses-compatible backends and models are candidates, not verified integrations.
- Chat Completions-only endpoints and WebSocket transport are not implemented by this adapter.
- Linux and macOS use POSIX process/file-lock behavior. On Windows use WSL; native Windows installation is unsupported.
- Native editing does not mean every cloud-model tool or service is available locally.
- Namespace structures are preserved, not flattened. Generic custom tools are translated but only native patch editing has live validation.
- The default installation retains Codex's sandbox and approval policy; the host must support that sandbox.

When reporting a new backend, provide CLI/Python/server versions, the model and parser, and whether the live smoke test passed. Do not include credentials or unredacted private logs.
