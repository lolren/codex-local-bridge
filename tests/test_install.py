import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import tomllib
import unittest

import install
from launcher import ensure_running, health, stop_running
from scripts.enable_native_editing import enable


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex install test ")
        self.root = Path(self.temp.name)
        self.codex = self.root / "fake-codex"
        self.codex.write_text(f'''#!{sys.executable}
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.157.1")
else:
    print(json.dumps({{"argv": sys.argv[1:], "home": os.environ.get("CODEX_HOME")}}))
''')
        self.codex.chmod(0o755)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.args = ["--upstream", "http://127.0.0.1:8000/v1", "--model", "organization/model-id",
                     "--prefix", str(self.root / "installed"), "--port", str(self.port), "--codex", str(self.codex)]
        self.paths = install.layout(str(self.root / "installed"))
        self.running = None

    def tearDown(self):
        if self.running:
            stop_running(self.running)
        self.temp.cleanup()

    def run_install(self, extra=()):
        with contextlib.redirect_stdout(io.StringIO()):
            install.main(self.args + list(extra))

    def test_configuration_and_permissions(self):
        self.run_install(["--api-key-env", "LOCAL_LLM_API_KEY", "--context-window", "262144"])
        config = tomllib.loads((self.paths["home"] / "config.toml").read_text())
        self.assertEqual(config["model"], "organization/model-id")
        self.assertEqual(config["sandbox_mode"], "workspace-write")
        self.assertEqual(config["model_context_window"], 262144)
        provider = config["model_providers"]["local-bridge"]
        self.assertEqual(provider["env_key"], "LOCAL_LLM_API_KEY")
        self.assertFalse(provider["requires_openai_auth"])
        self.assertEqual(provider["wire_api"], "responses")
        catalog = json.loads(Path(config["model_catalog_json"]).read_text())
        self.assertEqual(catalog["models"][0]["apply_patch_tool_type"], "freeform")
        self.assertEqual((self.paths["home"] / "config.toml").stat().st_mode & 0o777, 0o600)
        self.assertTrue(os.access(self.paths["bin"], os.X_OK))

    def test_dry_run_has_no_writes(self):
        self.run_install(["--dry-run"])
        self.assertFalse(self.paths["data"].exists())
        self.assertFalse(self.paths["home"].exists())

    def test_overwrite_refused_and_force_backs_up(self):
        self.run_install()
        config = self.paths["home"] / "config.toml"
        config.write_text(config.read_text() + "\n# local customization\n")
        before = config.read_bytes()
        with self.assertRaises(SystemExit):
            self.run_install()
        self.assertEqual(config.read_bytes(), before)
        self.run_install(["--force"])
        backups = list((self.paths["data"] / "backups").glob("*/index.json"))
        self.assertEqual(len(backups), 1)
        index = json.loads(backups[0].read_text())
        saved = next(key for key, value in index.items() if value == str(config))
        self.assertEqual((backups[0].parent / saved).read_bytes(), before)

    def test_launcher_starts_adapter_and_sets_provider_and_home(self):
        self.run_install()
        settings = json.loads((self.paths["data"] / "installation.json").read_text())
        self.running = settings
        output = subprocess.check_output([str(self.paths["bin"]), "--yolo", "exec", "hello"], text=True)
        result = json.loads(output)
        self.assertEqual(result["home"], str(self.paths["home"]))
        self.assertIn('model_provider="local-bridge"', result["argv"])
        self.assertIn("--no-daemon", result["argv"])
        self.assertEqual(result["argv"][-3:], ["--yolo", "exec", "hello"])
        self.assertEqual(health(settings)["instance_id"], settings["instance_id"])
        pid = health(settings)["pid"]
        ensure_running(settings)
        self.assertEqual(health(settings)["pid"], pid)

    def test_different_adapter_instance_is_not_reused(self):
        self.run_install()
        settings = json.loads((self.paths["data"] / "installation.json").read_text())
        self.running = settings
        ensure_running(settings)
        with self.assertRaises(RuntimeError):
            health({**settings, "instance_id": "wrong-instance"})

    def test_manual_catalog_patch_is_targeted_idempotent_and_backed_up(self):
        path = self.root / "models.json"
        original = {"models": [{"slug": "a"}, {"slug": "b", "description": "keep"}]}
        path.write_text(json.dumps(original))
        self.assertEqual(enable(path, "b", dry_run=True), "would enable freeform native editing")
        self.assertEqual(json.loads(path.read_text()), original)
        backup = enable(path, "b")
        self.assertEqual(json.loads(backup.read_text()), original)
        changed = json.loads(path.read_text())
        self.assertEqual(changed["models"][0], original["models"][0])
        self.assertEqual(changed["models"][1]["apply_patch_tool_type"], "freeform")
        self.assertIsNone(enable(path, "b"))
        with self.assertRaises(ValueError):
            enable(path, "missing")
