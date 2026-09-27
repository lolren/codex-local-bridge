# Security boundary

The adapter is a single-user loopback service. Do not expose it as a public reverse proxy. It forwards prompts, tool results, and the client's bearer header to the configured upstream, and never executes model tools itself. Codex executes those tools under its configured permissions.

`--yolo` disables Codex's approval prompts and sandbox. It is optional and is never the installer's default. A temporary smoke-test directory does not isolate the rest of the machine from an unrestricted process.

The adapter omits request bodies and credentials from its logs; upstream and Codex logs have their own policies. Session logs, local configuration backups, and environment files should not be committed.

For a security report, use GitHub's private vulnerability reporting feature if enabled on this repository. Otherwise open a minimal issue requesting a private reporting channel without publishing credentials, private data, or an exploit against a live installation.
