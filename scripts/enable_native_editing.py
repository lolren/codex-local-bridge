#!/usr/bin/env python3
"""Enable the native patch capability in an existing Codex model catalog."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install import atomic_write


def enable(path, model, dry_run=False):
    path = Path(path).expanduser()
    catalog = json.loads(path.read_text())
    matches = [entry for entry in catalog["models"] if entry.get("slug") == model]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one catalog entry for {model!r}; found {len(matches)}")
    if matches[0].get("apply_patch_tool_type") == "freeform":
        return None
    matches[0]["apply_patch_tool_type"] = "freeform"
    if dry_run:
        return "would enable freeform native editing"
    backup = path.with_name(path.name + ".bak-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    shutil.copy2(path, backup)
    atomic_write(path, json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", path.stat().st_mode & 0o777)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = enable(args.catalog, args.model, args.dry_run)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")
    print(result or "Already enabled; no changes made.")
    print("Use the Responses adapter as well if the backend only exposes function tools.")


if __name__ == "__main__":
    main()
