"""Launch Codex with an isolated local provider and an automatically started adapter."""

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

HTTP = build_opener(ProxyHandler({}))
# Retain/reap children when these helpers are used without exec (doctor and tests).
CHILDREN = {}


def health(config):
    try:
        with HTTP.open(f"http://127.0.0.1:{config['port']}/health", timeout=1) as response:
            info = json.load(response)
    except (OSError, URLError):
        return None
    except (ValueError, TypeError) as exc:
        raise RuntimeError("The configured port is occupied by a different service") from exc
    if (not isinstance(info, dict) or info.get("service") != "codex-local-bridge"
            or info.get("instance_id") != config["instance_id"]):
        raise RuntimeError("A different adapter/service occupies this port; select another --port")
    return info


def ensure_running(config):
    data = Path(config["data_dir"])
    with (data / "startup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if health(config):
            return
        process = None
        if config.get("systemd"):
            subprocess.run(["systemctl", "--user", "start", config["unit"]], check=True)
        else:
            log = data / "bridge.log"
            # Bound retained diagnostics; request bodies and credentials are never logged.
            if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
                log.replace(data / "bridge.log.1")
            with log.open("ab") as stream:
                process = subprocess.Popen(
                    [config["python"], str(data / "bridge.py"), "--upstream", config["upstream"],
                     "--port", str(config["port"]), "--instance-id", config["instance_id"]],
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                    start_new_session=True, close_fds=True,
                )
                CHILDREN[process.pid] = process
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if health(config):
                return
            if process is not None and process.poll() is not None:
                break
            time.sleep(0.1)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise RuntimeError(f"Adapter did not start; check {data / 'bridge.log'} or the user service journal")


def stop_running(config):
    if config.get("systemd"):
        subprocess.run(["systemctl", "--user", "stop", config["unit"]], check=True)
    else:
        info = health(config)
        if info:
            os.kill(info["pid"], signal.SIGTERM)
            child = CHILDREN.pop(info["pid"], None)
            if child:
                child.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if health(config) is None:
            return
        time.sleep(0.1)
    raise RuntimeError("Adapter did not stop within 10 seconds")


def main(config_path, args=None):
    config = json.loads(Path(config_path).read_text())
    args = sys.argv[1:] if args is None else args
    try:
        if args == ["--bridge-stop"]:
            stop_running(config)
            return
        if args == ["--bridge-status"]:
            print(json.dumps(health(config) or {"status": "stopped"}, indent=2))
            return
        ensure_running(config)
        env = os.environ.copy()
        env["CODEX_HOME"] = config["codex_home"]
        command = [config["codex"], "--no-daemon", "--profile", "local",
                   "--model", config["model"], "-c", 'model_provider="local-bridge"', *args]
        os.execve(config["codex"], command, env)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"codex-local: {exc}", file=sys.stderr)
        raise SystemExit(1)
