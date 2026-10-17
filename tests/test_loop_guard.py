import copy
import json
import unittest

from bridge import to_upstream
from loop_guard import ToolLoopError


def history(repeats, *, calls=None, result="unchanged failure"):
    calls = calls or [("exec_command", {"cmd": "retry"})]
    items = [{"role": "user", "content": "Fix the failure."}]
    for turn in range(repeats):
        for index, (name, arguments) in enumerate(calls):
            call_id = f"c{turn}-{index}"
            items += [{"type": "function_call", "name": name, "call_id": call_id,
                       "arguments": json.dumps(arguments)},
                      {"type": "function_call_output", "call_id": call_id, "output": result}]
    return {"model": "local", "input": items}


class LoopGuardTests(unittest.TestCase):
    def test_opt_in_soft_and_hard_thresholds(self):
        for repeats in (1, 2, 3):
            result, _ = to_upstream(history(repeats), loop_guard=True)
            self.assertNotIn("repetition_penalty", result)
        request = history(4)
        original = copy.deepcopy(request)
        result, _ = to_upstream(request, loop_guard=True)
        self.assertEqual(result["repetition_penalty"], 1.1)
        self.assertEqual(result["penalty_last_n"], 512)
        self.assertEqual(result["input"], original["input"])
        self.assertEqual(request, original)
        self.assertEqual(to_upstream(history(6))[0], history(6))
        with self.assertRaises(ToolLoopError):
            to_upstream(history(6), loop_guard=True)

    def test_short_cycles_and_explicit_parameters(self):
        request = history(4, calls=[("exec_command", {"cmd": "repair"}), ("exec_command", {"cmd": "check"})])
        request.update(repetition_penalty=1., penalty_last_n=64, temperature=0)
        result, _ = to_upstream(request, loop_guard=True)
        self.assertEqual(result["repetition_penalty"], 1.)
        self.assertEqual(result["penalty_last_n"], 64)
        self.assertEqual(result["temperature"], 0)
        with self.assertRaises(ToolLoopError):
            to_upstream(history(6, calls=[("exec_command", {"cmd": "repair"}),
                                         ("exec_command", {"cmd": "check"})]), loop_guard=True)

    def test_changed_result_arguments_or_new_user_message_reset(self):
        for change in ("result", "arguments", "user"):
            request = history(6)
            if change == "result": request["input"][-1]["output"] = "fixed"
            elif change == "arguments": request["input"][-2]["arguments"] = '{"cmd":"new approach"}'
            else: request["input"].append({"role": "user", "content": "Repeat intentionally."})
            result, _ = to_upstream(request, loop_guard=True)
            self.assertNotIn("repetition_penalty", result)

    def test_arguments_are_compared_as_json_not_key_order(self):
        request = history(6, calls=[("exec_command", {"cmd": "retry", "cwd": "/tmp"})])
        request["input"][-2]["arguments"] = '{ "cwd": "/tmp", "cmd": "retry" }'
        with self.assertRaises(ToolLoopError): to_upstream(request, loop_guard=True)

    def test_execution_metadata_changes_do_not_hide_loop(self):
        for structured in (True, False):
            request = history(6)
            for index, item in enumerate(request["input"]):
                if item.get("type") == "function_call_output":
                    if structured:
                        item["output"] = json.dumps({"chunk_id": str(index), "wall_time_seconds": index,
                                                     "exit_code": 1, "output": "same failure"})
                    else:
                        item["output"] = (f"Chunk ID: {index}\nWall time: {index} seconds\n"
                                          "Process exited with code 1\nFinal output:\nsame failure\n")
            with self.assertRaises(ToolLoopError): to_upstream(request, loop_guard=True)

    def test_polling_and_running_processes_are_not_loops(self):
        for name in ("write_stdin", "functions.wait", "clock__sleep", "view_image"):
            request = history(9, calls=[(name, {"session_id": 1})])
            self.assertNotIn("repetition_penalty", to_upstream(request, loop_guard=True)[0])
        request = history(9, result=json.dumps({"session_id": 1, "exit_code": None, "output": ""}))
        self.assertNotIn("repetition_penalty", to_upstream(request, loop_guard=True)[0])

    def test_native_patch_history_and_images_still_pass_through(self):
        request = history(4, calls=[("apply_patch", {"input": "*** Begin Patch\n*** End Patch"})])
        for item in request["input"]:
            if item.get("type") == "function_call":
                item["type"] = "custom_tool_call"
                item["input"] = json.loads(item.pop("arguments"))["input"]
            elif item.get("type") == "function_call_output": item["type"] = "custom_tool_call_output"
        request["input"].insert(1, {"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,TEST"}]})
        result, names = to_upstream(request, loop_guard=True)
        self.assertEqual(result["repetition_penalty"], 1.1)
        self.assertEqual(result["input"][1], request["input"][1])
        self.assertEqual(names, {"apply_patch"})
