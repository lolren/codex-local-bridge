"""Opt-in detection of unchanged tool-call loops from the Responses history.

No prompts or tool results are rewritten or logged. The guard is stateless,
so separate sessions cannot affect each other and a new user message resets it.
"""

import hashlib
import json


SOFT_REPEATS = 4
HARD_REPEATS = 6
MAX_CYCLE = 4
POLLING_TOOLS = {"write_stdin", "wait", "sleep", "request_user_input", "request_user_input_async"}


class ToolLoopError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def stable_result(value):
    """Ignore execution envelope timings, retaining exit status and actual output.

    A still-running process is progress that a polling tool must be allowed to
    wait for. Arbitrary text (including real output mentioning times) is kept.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return stable_result(parsed)
        lines = value.splitlines(keepends=True)
        if lines and lines[0].startswith(("Chunk ID:", "Wall time:")):
            split = next((i for i, line in enumerate(lines)
                          if line.strip() in ("Output:", "Final output:")), None)
            if split is not None:
                header = lines[:split]
                if any(line.startswith(("Process running with session ID", "Script running with cell ID"))
                       for line in header):
                    return None
                status = [line.strip() for line in header
                          if line.startswith(("Process exited with code", "Exit code:"))]
                return {"status": status, "output": "".join(lines[split + 1:])}
        return value
    if isinstance(value, dict) and "output" in value and any(
            key in value for key in ("exit_code", "wall_time_seconds", "session_id", "chunk_id")):
        if value.get("session_id") is not None and value.get("exit_code") is None:
            return None
        return {key: val for key, val in value.items()
                if key not in ("chunk_id", "wall_time_seconds", "original_token_count")}
    return value


def completed_fingerprints(items):
    calls, outputs = [], {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            calls, outputs = [], {}
        elif item.get("type") == "function_call":
            calls.append(item)
        elif item.get("type") == "function_call_output":
            outputs[item.get("call_id")] = item.get("output")
    fingerprints = []
    for call in calls:
        call_id, name = call.get("call_id"), call.get("name")
        if not isinstance(name, str) or not call_id or call_id not in outputs:
            fingerprints = []
            continue
        base_name = name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
        if base_name in POLLING_TOOLS or base_name == "view_image":
            fingerprints = []
            continue
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        result = stable_result(outputs[call_id])
        if result is None:
            fingerprints = []
            continue
        record = (call.get("namespace"), name, arguments, result)
        fingerprints.append(hashlib.sha256(canonical(record).encode("utf-8")).digest())
    return fingerprints[-MAX_CYCLE * HARD_REPEATS:]


def repeated_cycle(fingerprints):
    best = (0, 0)
    for width in range(1, min(MAX_CYCLE, len(fingerprints) // SOFT_REPEATS) + 1):
        tail, repeats = fingerprints[-width:], 1
        offset = len(fingerprints) - 2 * width
        while offset >= 0 and fingerprints[offset:offset + width] == tail:
            repeats += 1
            offset -= width
        if repeats > best[0]:
            best = (repeats, width)
    return best


def apply_loop_guard(payload):
    items = payload.get("input")
    if not isinstance(items, list):
        return
    repeats, width = repeated_cycle(completed_fingerprints(items))
    if repeats >= HARD_REPEATS:
        raise ToolLoopError(
            f"Stopped an unchanged tool loop: a cycle of {width} call(s) repeated {repeats} times "
            "with identical arguments and results. Send a new instruction to change approach and continue.")
    if repeats >= SOFT_REPEATS:
        # Respect explicit client settings; absent/null fields inherit this
        # request-only recovery attempt instead of the server defaults.
        if payload.get("repetition_penalty") is None:
            payload["repetition_penalty"] = 1.1
        if payload.get("penalty_last_n") is None:
            payload["penalty_last_n"] = 512
