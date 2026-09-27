# Contributing

Use Python 3.11+; no external packages are needed. Keep the bridge focused on preserving Codex tool semantics across a Responses/function-tool backend.

```bash
python3 -m unittest discover -v
```

Protocol changes should test request history as well as both streaming and non-streaming results. Changes to startup or installation should preserve instance checks, isolated configuration, and overwrite backups. Keep credentials, private hosts, prompts, and generated session logs out of fixtures and commits.

For a compatibility report, include Codex, Python, and backend versions; model/parser configuration; the relevant redacted error; and the result of `scripts/smoke_test.py`. Distinguish automated protocol coverage from a real model run. The optional live test consumes inference resources and is not run in hosted CI.

Please make source links version-specific when describing Codex internals. This is an independent integration project; upstream API behavior can change.
