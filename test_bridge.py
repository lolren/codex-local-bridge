import copy
import io
import json
import unittest

from bridge import EventTranslator, restore_response, sse_data, to_upstream


PATCH = '*** Begin Patch\n*** Add File: café.txt\n+"hello"\\world\n*** End Patch\n'


class ProtocolTests(unittest.TestCase):
    def test_request_preserves_history_and_ordinary_tools(self):
        request = {
            "tools": [{"type": "custom", "name": "apply_patch", "format": {"type": "grammar"}},
                      {"type": "function", "name": "exec_command", "parameters": {}}],
            "input": [{"type": "custom_tool_call", "name": "apply_patch", "call_id": "c1", "input": PATCH},
                      {"type": "custom_tool_call_output", "call_id": "c1", "output": "Success"},
                      {"role": "user", "content": "Now inspect it"}],
            "tool_choice": {"type": "custom", "name": "apply_patch"},
        }
        original = copy.deepcopy(request)
        converted, names = to_upstream(request)
        self.assertEqual(request, original)
        self.assertEqual(names, {"apply_patch"})
        self.assertEqual(converted["tools"][0]["type"], "function")
        self.assertEqual(converted["tools"][1], request["tools"][1])
        self.assertEqual(json.loads(converted["input"][0]["arguments"]), {"input": PATCH})
        self.assertEqual(converted["input"][1]["type"], "function_call_output")
        self.assertEqual(converted["input"][2], request["input"][2])
        self.assertEqual(converted["tool_choice"]["type"], "function")

    def test_namespace_and_auto_choice(self):
        request = {"tools": [{"type": "namespace", "name": "functions", "tools": [
            {"type": "custom", "name": "apply_patch"}]}], "tool_choice": "auto"}
        converted, names = to_upstream(request)
        self.assertEqual(converted["tool_choice"], "auto")
        self.assertEqual(converted["tools"][0]["tools"][0]["name"], "apply_patch")
        self.assertEqual(names, {"apply_patch"})

    def test_stream_decodes_split_json_exactly_once(self):
        raw = json.dumps({"input": PATCH})
        item = {"id": "fc1", "call_id": "call1", "type": "function_call", "name": "apply_patch", "arguments": ""}
        translator = EventTranslator({"apply_patch"})
        events = translator.process({"type": "response.output_item.added", "output_index": 1, "item": item})
        for part in [raw[i:i+3] for i in range(0, len(raw), 3)]:
            events += translator.process({"type": "response.function_call_arguments.delta", "item_id": "fc1", "output_index": 1, "delta": part})
        events += translator.process({"type": "response.function_call_arguments.done", "item_id": "fc1", "output_index": 1, "arguments": raw})
        item["arguments"] = raw
        events += translator.process({"type": "response.output_item.done", "output_index": 1, "item": item})
        deltas = [e["delta"] for e in events if e["type"] == "response.custom_tool_call_input.delta"]
        self.assertEqual(deltas, [PATCH])
        self.assertEqual(events[0]["item"]["input"], "")
        self.assertEqual(events[-1]["item"]["input"], PATCH)
        self.assertEqual(events[-1]["item"]["call_id"], "call1")
        self.assertTrue(all("function_call_arguments" not in e["type"] for e in events))

    def test_done_only_stream_and_normal_function_passthrough(self):
        translator = EventTranslator({"apply_patch"})
        done = {"type": "response.output_item.done", "output_index": 0, "item": {
            "id": "fc1", "call_id": "c1", "type": "function_call", "name": "apply_patch", "arguments": json.dumps({"input": PATCH})}}
        events = translator.process(done)
        self.assertEqual(events[0]["delta"], PATCH)
        self.assertEqual(events[-1]["item"]["type"], "custom_tool_call")
        ordinary = copy.deepcopy(done)
        ordinary["item"]["name"] = "exec_command"
        self.assertEqual(translator.process(ordinary), [ordinary])

    def test_full_response_and_continuation_roundtrip(self):
        response = {"status": "completed", "output": [
            {"id": "fc1", "call_id": "c1", "type": "function_call", "name": "apply_patch", "arguments": json.dumps({"input": PATCH})},
            {"id": "m1", "type": "message", "content": [{"type": "output_text", "text": "done"}]}]}
        restored = restore_response(response, {"apply_patch"})
        self.assertEqual(restored["output"][0]["input"], PATCH)
        self.assertEqual(restored["output"][1], response["output"][1])
        converted, _ = to_upstream({"input": restored["output"]})
        self.assertEqual(json.loads(converted["input"][0]["arguments"]), {"input": PATCH})
        event = EventTranslator({"apply_patch"}).process({"type": "response.completed", "response": response})[0]
        self.assertEqual(event["response"], restored)

    def test_malformed_tool_arguments_fail_instead_of_becoming_patch(self):
        response = {"output": [{"type": "function_call", "name": "apply_patch", "arguments": '{"input":false}'}]}
        with self.assertRaises(ValueError):
            restore_response(response, {"apply_patch"})

    def test_sse_frames(self):
        raw = b': keepalive\r\nevent: test\r\ndata: {"type":\r\ndata: "test"}\r\n\r\ndata: [DONE]\n\n'
        frames = list(sse_data(io.BytesIO(raw)))
        self.assertEqual(json.loads(frames[0]), {"type": "test"})
        self.assertEqual(frames[1], "[DONE]")


if __name__ == "__main__":
    unittest.main()
