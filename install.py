#!/usr/bin/env python3
"""Install a separate Codex local-model profile without touching ~/.codex."""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid

from bridge import normalize_upstream

SOURCE = Path(__file__).resolve().parent
VERIFIED_CODEX_VERSION = "0.157.1"
INSTRUCTIONS = (
    "You are a coding agent running in Codex CLI. Follow system, developer, and repository "
    "instructions. Use available tools to inspect the project and make requested changes "
    "directly. Prefer the native apply_patch tool for creating, updating, and deleting files; "
    "use execution tools for reading files and running checks. Verify the result before "
    "reporting success. Ask only when essential information is missing."
)


def layout(prefix=None, name="codex-local"):
    if prefix:
        root = Path(prefix).expanduser().resolve()
        return {"data": root / "share/codex-local-bridge", "home": root / "config",
                "bin": root / "bin" / name, "unit": root / "systemd" / (name + "-bridge.service")}
    home = Path.home()
    return {"data": home / ".local/share/codex-local-bridge", "home": home / ".codex-local",
            "bin": home / ".local/bin" / name,
            "unit": home / ".config/systemd/user" / (name + "-bridge.service")}


def model_catalog(model, context, effort=None):
    return {"models": [{
        "slug": model, "display_name": model + " (local)",
        "description": "Local model with native Codex file editing.",
        "default_reasoning_level": effort,
        "supported_reasoning_levels": [{"effort": effort, "description": "Server-supported effort"}] if effort else [],
        "shell_type": "shell_command", "visibility": "list", "supported_in_api": True,
        "priority": 1, "model_messages": {"instructions_template": INSTRUCTIONS},
        "support_verbosity": False, "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text", "truncation_policy": {"mode": "bytes", "limit": 10000},
        "context_window": context, "max_context_window": context,
        "experimental_supported_tools": [],
    }]}


def toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def unit_quote(value):
    return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def atomic_write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.chmod(mode)
    temporary.replace(path)


def build_files(args, paths, codex, instance_id):
    data, home = paths["data"], paths["home"]
    settings = {
        "upstream": normalize_upstream(args.upstream), "port": args.port, "model": args.model,
        "context_window": args.context_window, "instance_id": instance_id, "codex": str(codex),
        "python": sys.executable, "data_dir": str(data), "codex_home": str(home),
        "systemd": args.systemd, "unit": paths["unit"].name, "api_key_env": args.api_key_env,
    }
    config = f'''model = {toml_string(args.model)}
model_provider = "local-bridge"
model_context_window = {args.context_window}
model_catalog_json = {toml_string(home / 'models.json')}
model_supports_reasoning_summaries = false
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[model_providers.local-bridge]
name = "Local model via native tool adapter"
base_url = "http://127.0.0.1:{args.port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
stream_idle_timeout_ms = 600000
'''
    if args.api_key_env:
        config += f"env_key = {toml_string(args.api_key_env)}\n"
    profile = f'model = {toml_string(args.model)}\nmodel_provider = "local-bridge"\n'
    if args.reasoning_effort:
        profile += f"model_reasoning_effort = {toml_string(args.reasoning_effort)}\n"
    tomllib.loads(config)
    tomllib.loads(profile)
    launcher = f'''#!/usr/bin/env python3
import sys
sys.path.insert(0, {str(data)!r})
from launcher import main
main({str(data / 'installation.json')!r})
'''
    command = " ".join(unit_quote(x) for x in [sys.executable, "-u", data / "bridge.py",
        "--upstream", settings["upstream"], "--port", args.port, "--instance-id", instance_id])
    service = f'''[Unit]
Description=Codex local model native tool adapter
After=network.target

[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
'''
    files = {
        home / "config.toml": (config, 0o600), home / "local.config.toml": (profile, 0o600),
        home / "models.json": (json.dumps(model_catalog(args.model, args.context_window, args.reasoning_effort), indent=2) + "\n", 0o600),
        data / "installation.json": (json.dumps(settings, indent=2) + "\n", 0o600),
        data / "bridge.py": ((SOURCE / "bridge.py").read_text(), 0o600),
        data / "launcher.py": ((SOURCE / "launcher.py").read_text(), 0o600),
        paths["bin"]: (launcher, 0o755),
    }
    if args.systemd:
        files[paths["unit"]] = (service, 0o600)
    return files


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="Responses-capable server root or /v1 URL")
    parser.add_argument("--model", required=True, help="Exact model id from GET /v1/models")
    parser.add_argument("--context-window", type=int, default=32768)
    parser.add_argument("--reasoning-effort", help="Only set a value your server supports")
    parser.add_argument("--api-key-env", help="Name of environment variable containing the server's API key")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--codex", help="Path to a Codex CLI executable")
    parser.add_argument("--prefix", help="Keep all generated files under this directory")
    parser.add_argument("--name", default="codex-local", help="Launcher name")
    parser.add_argument("--systemd", action="store_true", help="Enable a Linux user service at login")
    parser.add_argument("--force", action="store_true", help="Back up and replace existing generated files")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.context_window < 1024 or not 1 <= args.port <= 65535:
        parser.error("context window must be >=1024 and port must be 1..65535")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.name):
        parser.error("launcher name may contain letters, numbers, hyphens, and underscores")
    if args.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        parser.error("--api-key-env takes a variable name, not a token")
    if args.systemd and (args.prefix or sys.platform != "linux"):
        parser.error("--systemd requires Linux and the default per-user paths (no --prefix)")
    if os.name != "posix":
        parser.error("use Linux, macOS, or WSL for this installer")
    return args


def main(argv=None):
    args = parse_args(argv)
    paths = layout(args.prefix, args.name)
    found = (shutil.which(args.codex) or args.codex) if args.codex else shutil.which("codex")
    if not found:
        raise SystemExit(f"Install Codex first: npm install -g @openai/codex@{VERIFIED_CODEX_VERSION}")
    codex = Path(found).expanduser().absolute()
    result = subprocess.run([str(codex), "--version"], capture_output=True, text=True, check=True)
    print(result.stdout.strip())
    if VERIFIED_CODEX_VERSION not in result.stdout:
        print(f"Verified baseline is Codex {VERIFIED_CODEX_VERSION}; run the smoke test with your version.")
    if args.systemd:
        subprocess.run(["systemctl", "--user", "show-environment"], stdout=subprocess.DEVNULL, check=True)
    files = build_files(args, paths, codex, uuid.uuid4().hex)
    existing = [p for p in files if p.exists()]
    if existing and not args.force:
        raise SystemExit("Existing files would be overwritten; use --force for a timestamped backup:\n" + "\n".join(map(str, existing)))
    for path in files:
        print(("Would write: " if args.dry_run else "Install: ") + str(path))
    if args.dry_run:
        return
    if existing:
        settings_path = paths["data"] / "installation.json"
        if settings_path.exists():
            from launcher import stop_running
            old_settings = json.loads(settings_path.read_text())
            stop_running(old_settings)
            if old_settings.get("systemd") and not args.systemd:
                subprocess.run(["systemctl", "--user", "disable", old_settings["unit"]], check=True)
        backup = paths["data"] / "backups" / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup.mkdir(parents=True, mode=0o700)
        index = {}
        for number, path in enumerate(existing):
            name = f"{number}-{path.name}"
            shutil.copy2(path, backup / name)
            index[name] = str(path)
        atomic_write(backup / "index.json", json.dumps(index, indent=2) + "\n")
        print("Backup:", backup)
    for path, (text, mode) in files.items():
        atomic_write(path, text, mode)
    if args.systemd:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", paths["unit"].name], check=True)
    print(f"\nInstalled. Start a new session in your project:\n  {paths['bin']}")
    print("The adapter starts automatically; no cloud login is needed for an unauthenticated local server.")


if __name__ == "__main__":
    main()
