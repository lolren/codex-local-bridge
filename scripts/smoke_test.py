#!/usr/bin/env python3
"""Prove that a real Codex session performs native add/update/delete operations."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

TESTS = '''import unittest
from calculator import add

class CalculatorTests(unittest.TestCase):
    def test_positive(self): self.assertEqual(add(2, 3), 5)
    def test_negative(self): self.assertEqual(add(-2, -3), -5)
    def test_zero(self): self.assertEqual(add(4, 0), 4)
'''

PROMPT = (
    "Fix calculator.py so add(a, b) returns the sum, create README.md with a short "
    "description and the test command, and delete obsolete.txt. Inspect the files "
    "first, leave test_calculator.py unchanged, run the existing unit tests, and "
    "report the result. Make the changes yourself."
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", default="codex-local")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--yolo", action="store_true", help="Opt in to disabling Codex approvals and sandbox")
    parser.add_argument("--save-log", type=Path, help="Optional private JSONL output for diagnosis")
    args = parser.parse_args()
    executable = shutil.which(args.launcher)
    if not executable:
        parser.exit(1, f"Launcher not found: {args.launcher}\n")
    with tempfile.TemporaryDirectory(prefix="codex-local-smoke-") as directory:
        root = Path(directory)
        (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        (root / "test_calculator.py").write_text(TESTS)
        (root / "obsolete.txt").write_text("Remove this obsolete file.\n")
        original_tests = hashlib.sha256((root / "test_calculator.py").read_bytes()).hexdigest()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        command = [executable]
        if args.yolo:
            command += ["--yolo"]
        command += ["exec", "--ephemeral", "--json", "-C", str(root), PROMPT]
        print("Running a real model session in a temporary project...", flush=True)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            if args.save_log:
                save_log(args.save_log, stdout)
            parser.exit(1, "Model session timed out; inspect server readiness, tools, and context settings.\n")
        if args.save_log:
            save_log(args.save_log, stdout)
        events = []
        for line in stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        changes = [event["item"] for event in events if event.get("type") == "item.completed"
                   and event.get("item", {}).get("type") == "file_change"
                   and event["item"].get("status") == "completed"]
        kinds = {change["kind"] for item in changes for change in item.get("changes", [])}
        # Check actual behavior independently of the model's final message.
        checks = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"],
                                cwd=root, capture_output=True, text=True)
        unchanged = (root / "test_calculator.py").exists() and hashlib.sha256((root / "test_calculator.py").read_bytes()).hexdigest() == original_tests
        readme = (root / "README.md").is_file() and (root / "README.md").stat().st_size > 0
        failures = []
        if process.returncode: failures.append(f"Codex exit status {process.returncode}")
        if not {"add", "update", "delete"}.issubset(kinds): failures.append(f"Native change kinds observed: {sorted(kinds)}; expected add/update/delete")
        if checks.returncode or not unchanged: failures.append("Independent tests failed or the test file changed")
        if not readme or (root / "obsolete.txt").exists(): failures.append("Expected file creation/deletion did not happen")
        if not any(event.get("type") == "turn.completed" for event in events): failures.append("No completed Codex turn")
        if failures:
            print("\n".join(failures), file=sys.stderr)
            print(stderr[-3000:], file=sys.stderr)
            print(checks.stderr[-2000:], file=sys.stderr)
            parser.exit(1, "Smoke test failed. Use --save-log to inspect the tool events locally.\n")
        print("PASS: native add/update/delete events; expected files; 3 unchanged unit tests pass.")


def save_log(path, text):
    # Logs may contain workspace paths or prompts. Never add them to the repository.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


if __name__ == "__main__":
    main()
