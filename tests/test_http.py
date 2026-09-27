import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from bridge import UPSTREAM, make_server, normalize_upstream, sse_data

PATCH = "*** Begin Patch\n*** Add File: café.txt\n+hello\n*** End Patch\n"


class MockUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.path, dict(self.headers), json.loads(data) if data else None))
        status, content_type, body = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status == 302:
            self.send_header("Location", "/unexpected-redirect")
        self.end_headers()
        self.wfile.write(body)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
        self.upstream.reply = (200, "application/json", b'{"data":[{"id":"local-model"}]}')
        self.upstream.seen = []
        self.bridge = make_server(f"http://127.0.0.1:{self.upstream.server_port}/v1", port=0, instance_id="test")
        self.threads = []
        for server in (self.upstream, self.bridge):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        for server in (self.bridge, self.upstream):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def request(self, path, data=None, headers=None):
        request = Request(f"http://127.0.0.1:{self.bridge.server_port}{path}", data=data,
                          headers=headers or {})
        try:
            with UPSTREAM.open(request, timeout=3) as response:
                return response.status, response.read()
        except HTTPError as response:
            with response:
                return response.code, response.read()

    def test_health_is_local_and_contains_instance(self):
        status, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["instance_id"], "test")
        self.assertEqual(self.upstream.seen, [])

    def test_model_passthrough_and_bearer_header(self):
        status, body = self.request("/v1/models", headers={"Authorization": "Bearer test-placeholder"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "local-model")
        self.assertEqual(self.upstream.seen[0][1]["Authorization"], "Bearer test-placeholder")

    def test_full_response_and_history_translation(self):
        self.upstream.reply = (200, "application/json", json.dumps({"output": [{
            "type": "function_call", "name": "apply_patch", "call_id": "c1", "id": "f1",
            "arguments": json.dumps({"input": PATCH})}]}).encode())
        payload = {"model": "local-model", "tools": [{"type": "custom", "name": "apply_patch"}],
                   "input": [{"type": "custom_tool_call", "name": "apply_patch", "call_id": "c0", "input": PATCH},
                             {"type": "custom_tool_call_output", "call_id": "c0", "output": "ok"}]}
        status, body = self.request("/v1/responses", json.dumps(payload).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["output"][0]["input"], PATCH)
        self.assertEqual(self.upstream.seen[0][2]["tools"][0]["type"], "function")
        self.assertEqual(self.upstream.seen[0][2]["input"][1]["type"], "function_call_output")

    def test_stream_is_restored_and_renumbered(self):
        item = {"id": "f1", "call_id": "c1", "type": "function_call", "name": "apply_patch", "arguments": ""}
        arguments = json.dumps({"input": PATCH})
        events = [{"type": "response.output_item.added", "item": item, "output_index": 0},
                  {"type": "response.function_call_arguments.delta", "item_id": "f1", "delta": arguments},
                  {"type": "response.function_call_arguments.done", "item_id": "f1", "arguments": arguments},
                  {"type": "response.output_item.done", "item": {**item, "arguments": arguments}, "output_index": 0}]
        body = b"".join(("data: " + json.dumps(e) + "\r\n\r\n").encode() for e in events)
        self.upstream.reply = (200, "text/event-stream", body + b"data: [DONE]\n\n")
        payload = {"stream": True, "tools": [{"type": "custom", "name": "apply_patch"}]}
        status, raw = self.request("/v1/responses", json.dumps(payload).encode())
        import io
        output = [json.loads(frame) for frame in sse_data(io.BytesIO(raw)) if frame != "[DONE]"]
        self.assertEqual(status, 200)
        self.assertEqual([e["sequence_number"] for e in output], list(range(len(output))))
        self.assertEqual([e["delta"] for e in output if e["type"].endswith("input.delta")], [PATCH])
        self.assertEqual(output[-1]["item"]["type"], "custom_tool_call")

    def test_invalid_requests_do_not_reach_upstream(self):
        for body in (b"not-json", b"[]", b'{"tools":[false]}'):
            self.assertEqual(self.request("/v1/responses", body)[0], 400)
        self.assertEqual(self.request("/unknown")[0], 404)
        self.assertEqual(self.upstream.seen, [])

    def test_http_errors_preserved(self):
        self.upstream.reply = (401, "application/json", b'{"error":{"message":"Unauthorized"}}')
        self.assertEqual(self.request("/v1/models"), (401, self.upstream.reply[2]))

    def test_redirects_are_not_followed(self):
        self.upstream.reply = (302, "application/json", b'{}')
        self.assertEqual(self.request("/v1/models")[0], 302)
        self.assertEqual(len(self.upstream.seen), 1)

    def test_malformed_function_arguments_fail_closed(self):
        self.upstream.reply = (200, "application/json", b'{"output":[{"type":"function_call","name":"apply_patch","arguments":"{}"}]}')
        self.assertEqual(self.request("/v1/responses", b'{"tools":[{"type":"custom","name":"apply_patch"}]}')[0], 502)


class UrlTests(unittest.TestCase):
    def test_listener_startup_does_not_require_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected DNS lookup")):
            server = make_server("http://127.0.0.1:8000", port=0)
            try:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)
            finally:
                server.server_close()

    def test_normalize(self):
        self.assertEqual(normalize_upstream("http://localhost:8000/v1/"), "http://localhost:8000")
        self.assertEqual(normalize_upstream("https://example.com/prefix/v1"), "https://example.com/prefix")

    def test_bad_urls(self):
        for value in ("file:///tmp/foo", "http://u:p@localhost", "http://localhost?token=x", "http://localhost/#fragment", "http://localhost:bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_upstream(value)
