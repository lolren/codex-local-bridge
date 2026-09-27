# Changelog

## 1.0.0 — 2026-09-27

- Enable Codex's native freeform patch tool for a custom served model.
- Translate custom tools and history to function tools, and restore native JSON/SSE responses.
- Add an isolated local-provider installer and a launcher with automatic adapter startup.
- Include manual configuration, reviewable patches, optional systemd support, diagnostics, and backup-aware upgrades.
- Add protocol, HTTP, installer, and launcher tests plus a real model add/update/delete smoke test.
- Document the reported native patch behaviors and source-checked transaction limits.

Verified CLI baseline: Codex 0.157.1. See [compatibility](docs/compatibility.md) for the live validation and its limits.
