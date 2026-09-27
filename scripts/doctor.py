#!/usr/bin/env python3
"""Check installation and model routing without asking the model to generate."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import UPSTREAM
from install import layout, VERIFIED_CODEX_VERSION
from launcher import health, ensure_running


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix")
    parser.add_argument("--start-bridge", action="store_true")
    args = parser.parse_args()
    try:
        paths = layout(args.prefix)
        settings = json.loads((paths["data"] / "installation.json").read_text())
        version = subprocess.check_output([settings["codex"], "--version"], text=True).strip()
        print("Codex:", version, f"(tested baseline: {VERIFIED_CODEX_VERSION})")
        config = tomllib.loads((paths["home"] / "config.toml").read_text())
        catalog = json.loads(Path(config["model_catalog_json"]).read_text())
        entry = next(m for m in catalog["models"] if m["slug"] == settings["model"])
        assert entry.get("apply_patch_tool_type") == "freeform", "Catalog lacks freeform native editing"
        assert config["model_provider"] == "local-bridge", "Wrong default provider"
        provider = config["model_providers"]["local-bridge"]
        assert provider["base_url"] == f"http://127.0.0.1:{settings['port']}/v1", "Wrong bridge URL"
        assert provider["wire_api"] == "responses" and not provider.get("requires_openai_auth"), "Wrong provider transport/auth"
        if args.start_bridge:
            ensure_running(settings)
        status = health(settings)
        print("Adapter:", "running" if status else "stopped (use --start-bridge or run the launcher)")
        headers = {}
        env_key = settings.get("api_key_env")
        if env_key:
            if not os.environ.get(env_key):
                raise ValueError(f"Set the {env_key} environment variable before starting Codex")
            headers["Authorization"] = "Bearer " + os.environ[env_key]
        request = Request(settings["upstream"] + "/v1/models", headers=headers)
        with UPSTREAM.open(request, timeout=15) as response:
            available = json.load(response)["data"]
        model = next((m for m in available if m["id"] == settings["model"]), None)
        if model is None:
            raise ValueError("Configured model is not advertised; available ids: " + ", ".join(m["id"] for m in available))
        if model.get("max_model_len") and settings["context_window"] > model["max_model_len"]:
            raise ValueError("Configured context exceeds the server's advertised max_model_len")
        print("Model:", settings["model"])
        print("Context:", settings["context_window"])
        print("Configuration and model discovery passed. Run smoke_test.py to verify Responses and native edits.")
    except (OSError, ValueError, KeyError, StopIteration, AssertionError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Check failed: {exc}\n")


if __name__ == "__main__":
    main()
