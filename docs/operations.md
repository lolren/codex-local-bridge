# Operating, upgrading, and removing an installation

## File locations

| Default path | Purpose |
| --- | --- |
| `~/.local/bin/codex-local` | Executable launcher |
| `~/.local/share/codex-local-bridge/bridge.py` | Installed adapter |
| `~/.local/share/codex-local-bridge/launcher.py` | Startup and routing implementation |
| `~/.local/share/codex-local-bridge/installation.json` | Installation metadata; no API key values |
| `~/.codex-local/config.toml` | Base local provider and permissions |
| `~/.codex-local/local.config.toml` | Selected model profile |
| `~/.codex-local/models.json` | Model metadata and native editing capability |
| `~/.config/systemd/user/codex-local-bridge.service` | Optional user service |

With `--prefix`, these become `PREFIX/bin/codex-local`, `PREFIX/share/codex-local-bridge/`, and `PREFIX/config/`. The default `~/.codex` is not modified. The isolated Codex home can acquire its own sessions and settings when you use it; keep it when uninstalling if you want that history.

## Startup

By default, the launcher starts a detached bridge on first use. It checks the listener's instance ID and serializes startup with a file lock to avoid duplicate processes. The bridge remains running after Codex exits and restarts on the next invocation if it has stopped.

```bash
codex-local --bridge-status
codex-local --bridge-stop
```

For a Linux user service, install with `--systemd`. The generated unit starts at user login and restarts on failure. It does not start the model server. The installer does not enable user lingering or change system-wide services.

```bash
systemctl --user status codex-local-bridge.service
systemctl --user restart codex-local-bridge.service
journalctl --user -u codex-local-bridge.service -n 80 --no-pager
```

A custom `--name` changes the unit to `NAME-bridge.service`. `--systemd` requires a working user service manager and default per-user installation paths.

## Multiple models or servers

For independently configured launchers, use a separate prefix, launcher name, and port:

```bash
python3 install.py --upstream http://127.0.0.1:8000/v1 --model MODEL_A \
  --prefix "$HOME/.local/opt/codex-model-a" --name codex-model-a --port 18081
python3 install.py --upstream http://127.0.0.1:9000/v1 --model MODEL_B \
  --prefix "$HOME/.local/opt/codex-model-b" --name codex-model-b --port 18082
```

Use the full launcher paths or add their `bin` directories to PATH. A different `--name` by itself does not create a separate configuration. The bridge does not load, unload, or switch GPU models for you; those are server operations.

## Upgrade

Keep Codex at the tested version unless you are deliberately validating another release. Model metadata, profile syntax, and SSE events can change across CLI versions.

```bash
git pull --ff-only
python3 -m unittest discover -v
# Repeat your original installer options, adding --force:
python3 install.py --upstream YOUR_SERVER_URL --model YOUR_SERVED_MODEL_ID --force
python3 scripts/doctor.py --start-bridge
python3 scripts/smoke_test.py
```

Repeat **all** relevant original options, including context, port, prefix, reasoning effort, token variable, and service choice. The installer does not infer omitted values from an existing installation. `--force` stops its old adapter, saves generated files under `share/codex-local-bridge/backups/TIMESTAMP/`, and installs the new files. This interrupts active adapter requests. It replaces generated configuration, so preserve/reapply custom additions as needed.

Without `--force`, existing generated paths cause the installer to stop without overwriting them. `--dry-run` shows destinations and writes nothing; when existing files are present, use `--force --dry-run` to preview a replacement.

Each backup contains an `index.json` mapping backup filenames to their original absolute destinations. To roll back, stop the bridge, inspect this index, and copy the selected backup files back to those destinations. For a service installation, reload the user manager and restart the restored unit. Backups can contain your customized configuration; keep them private.

## Remove

Close local sessions and stop the bridge. If you enabled the optional default user service, disable it before removing its unit:

```bash
codex-local --bridge-stop
# Service installations only:
systemctl --user disable --now codex-local-bridge.service
rm "$HOME/.config/systemd/user/codex-local-bridge.service"
systemctl --user daemon-reload
```

Remove the generated launcher and installed implementation files:

```bash
rm "$HOME/.local/bin/codex-local"
rm "$HOME/.local/share/codex-local-bridge/bridge.py"
rm "$HOME/.local/share/codex-local-bridge/launcher.py"
rm "$HOME/.local/share/codex-local-bridge/installation.json"
```

The remaining directory may contain logs, backups, and a startup lock. Remove those only if you no longer need them. Keep `~/.codex-local` for session history, or remove its generated configuration and history after reviewing it. For a custom prefix/name, substitute the paths printed by your installer. This does not uninstall Codex CLI or remove model weights.
