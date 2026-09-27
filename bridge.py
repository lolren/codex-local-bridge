#!/usr/bin/env python3
"""Translate Codex custom tools to vLLM function tools, preserving native edits."""

import argparse
import copy
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

LOG = logging.getLogger("codex-local-bridge")
VERSION = "1.0.0"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep bearer credentials on the explicitly configured upstream.
        return None


UPSTREAM = build_opener(ProxyHandler({}), NoRedirect())


def normalize_upstream(value):
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError("upstream must be an HTTP(S) URL without credentials, query, or fragment")
    parts.port  # Validate malformed ports before starting the server.
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


PATCH_DESCRIPTION = """Edit files using Codex's native apply_patch tool.
Send a JSON object with an input string containing the complete patch and real newlines.
The patch starts with *** Begin Patch and ends with *** End Patch.
For an existing file, use *** Update File: relative/path, then @@ and context lines
prefixed with a space, removed lines prefixed with -, and added lines prefixed with +.
For a new file, use *** Add File: relative/path and prefix every content line with +.
For deletion, use *** Delete File: relative/path. For renaming, use *** Update File:
followed by *** Move to: new/path and any changes. Use paths relative to the workspace.
Example input string:
*** Begin Patch
*** Update File: greeting.py
@@
-    return "Hello"
+    return "Welcome"
*** End Patch
Use this tool for file edits; use the shell for reading files and running checks.
"""


def to_upstream(payload):
    """Convert tool definitions AND history; never mutate the caller's request."""
    payload = copy.deepcopy(payload)
    custom_names = set()

    def convert_tool(tool):
        if tool.get("type") == "namespace":
            tool["tools"] = [convert_tool(t) for t in tool.get("tools", [])]
        elif tool.get("type") == "custom":
            name = tool["name"]
            custom_names.add(name)
            description = PATCH_DESCRIPTION if name == "apply_patch" else (
                "Call this tool using a JSON object with an input string holding "
                "its raw input. Tool purpose: " + tool.get("description", "")
            )
            tool = {
                "type": "function", "name": name, "description": description,
                "parameters": {
                    "type": "object", "properties": {"input": {"type": "string"}},
                    "required": ["input"], "additionalProperties": False,
                },
                "strict": True,
            }
        return tool

    if "tools" in payload:
        payload["tools"] = [convert_tool(t) for t in payload["tools"]]
    if isinstance(payload.get("input"), list):
        for item in payload["input"]:
            if item.get("type") == "custom_tool_call":
                custom_names.add(item["name"])
                item["type"] = "function_call"
                item["arguments"] = json.dumps({"input": item.pop("input")}, ensure_ascii=False)
            elif item.get("type") == "custom_tool_call_output":
                item["type"] = "function_call_output"
    choice = payload.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") == "custom":
            choice["type"] = "function"
        elif choice.get("type") == "allowed_tools":
            for tool in choice.get("tools", []):
                if tool.get("type") == "custom":
                    tool["type"] = "function"
    return payload, custom_names


def unpack_input(arguments):
    value = json.loads(arguments)
    if not isinstance(value, dict) or not isinstance(value.get("input"), str):
        raise ValueError("custom tool arguments must contain a string input field")
    return value["input"]


def restore_item(item, names, *, starting=False):
    item = copy.deepcopy(item)
    if item.get("type") == "function_call" and item.get("name") in names:
        arguments = item.pop("arguments", "")
        item["type"] = "custom_tool_call"
        item["input"] = "" if starting else unpack_input(arguments)
    return item


def restore_response(response, names):
    response = copy.deepcopy(response)
    if isinstance(response.get("output"), list):
        response["output"] = [restore_item(item, names) for item in response["output"]]
    return response


class EventTranslator:
    """Buffer JSON arguments, then emit the decoded patch as native custom input."""

    def __init__(self, names):
        self.names = names
        self.items = {}

    def input_events(self, item_id, output_index, raw):
        state = self.items[item_id]
        if state["sent"]:
            return []
        patch = unpack_input(raw)
        state["sent"] = True
        base = {"item_id": item_id, "output_index": output_index}
        return [
            {**base, "type": "response.custom_tool_call_input.delta", "delta": patch},
            {**base, "type": "response.custom_tool_call_input.done", "input": patch},
        ]

    def process(self, event):
        event = copy.deepcopy(event)
        kind = event.get("type", "")
        item = event.get("item", {})
        item_id = event.get("item_id")
        if kind in ("response.output_item.added", "response.output_item.done"):
            if item.get("type") == "function_call" and item.get("name") in self.names:
                item_id = item["id"]
                state = self.items.setdefault(item_id, {"arguments": "", "sent": False})
                if kind.endswith(".added"):
                    state["arguments"] = item.get("arguments", "")
                    event["item"] = restore_item(item, self.names, starting=True)
                else:
                    raw = item.get("arguments") or state["arguments"]
                    item["arguments"] = raw
                    extra = self.input_events(item_id, event.get("output_index", 0), raw)
                    event["item"] = restore_item(item, self.names)
                    LOG.info("restored native custom tool call: %s", item["name"])
                    return [*extra, event]
        elif item_id in self.items and kind == "response.function_call_arguments.delta":
            self.items[item_id]["arguments"] += event.get("delta", "")
            return []
        elif item_id in self.items and kind == "response.function_call_arguments.done":
            raw = event.get("arguments") or self.items[item_id]["arguments"]
            self.items[item_id]["arguments"] = raw
            return self.input_events(item_id, event.get("output_index", 0), raw)
        if isinstance(event.get("response"), dict):
            event["response"] = restore_response(event["response"], self.names)
        return [event]


def sse_data(stream):
    """Read SSE frames, including multiline data and CRLF from HTTP upstreams."""
    lines = []
    for line in stream:
        line = line.rstrip(b"\r\n")
        if not line:
            if lines:
                yield b"\n".join(lines).decode("utf-8")
                lines = []
        elif line.startswith(b"data:"):
            value = line[5:]
            lines.append(value[1:] if value.startswith(b" ") else value)
    if lines:
        yield b"\n".join(lines).decode("utf-8")


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexLocalBridge/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging URL query strings, prompts, headers, or tool arguments.
        LOG.info("%s %s", self.command, self.path.split("?", 1)[0])

    def send_body(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        self.proxy()

    def do_POST(self):
        self.proxy()

    def proxy(self):
        streaming = False
        try:
            if self.path == "/health":
                self.send_body(200, json.dumps({
                    "status": "ok", "service": "codex-local-bridge", "version": VERSION,
                    "instance_id": self.server.instance_id, "pid": os.getpid(),
                }).encode())
                return
            if not self.path.startswith("/v1/"):
                self.send_body(404, b'{"error":{"message":"Unknown route"}}')
                return
            names = set()
            data = None
            if self.command == "POST":
                if self.headers.get("Transfer-Encoding"):
                    self.send_body(400, b'{"error":{"message":"Content-Length is required"}}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_body(400, b'{"error":{"message":"Invalid Content-Length"}}')
                    return
                if not 0 <= length <= 32 * 1024 * 1024:
                    self.send_body(413, b'{"error":{"message":"Request too large"}}')
                    return
                data = self.rfile.read(length)
                if self.path.split("?", 1)[0] == "/v1/responses":
                    try:
                        payload = json.loads(data)
                        if not isinstance(payload, dict):
                            raise ValueError("expected an object")
                        payload, names = to_upstream(payload)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        self.send_body(400, b'{"error":{"message":"Invalid Responses request"}}')
                        return
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json", "Accept": self.headers.get("Accept", "application/json")}
            if "Authorization" in self.headers:
                headers["Authorization"] = self.headers["Authorization"]
            req = Request(self.server.upstream + self.path, data=data, headers=headers, method=self.command)
            with UPSTREAM.open(req, timeout=self.server.upstream_timeout) as upstream:
                content_type = upstream.headers.get("Content-Type", "application/json")
                if "text/event-stream" not in content_type:
                    body = upstream.read()
                    if names and "json" in content_type:
                        body = json.dumps(restore_response(json.loads(body), names), ensure_ascii=False).encode("utf-8")
                    self.send_body(upstream.status, body, content_type)
                    return
                self.send_response(upstream.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                streaming = True
                translator = EventTranslator(names)
                sequence = 0
                for raw in sse_data(upstream):
                    if raw == "[DONE]":
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                        continue
                    for event in translator.process(json.loads(raw)):
                        event["sequence_number"] = sequence
                        sequence += 1
                        self.send_event(event)
        except HTTPError as exc:
            with exc:
                self.send_body(exc.code, exc.read(), exc.headers.get("Content-Type", "application/json"))
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("client disconnected")
        except (ValueError, KeyError, TypeError, URLError, TimeoutError, OSError) as exc:
            LOG.error("bridge request failed: %s", type(exc).__name__)
            error = {"type": "local_bridge_error", "message": "Local tool adapter could not complete the request: " + type(exc).__name__}
            try:
                if streaming:
                    self.send_event({"type": "error", "error": error})
                else:
                    self.send_body(502, json.dumps({"error": error}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_event(self, event):
        frame = "event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()


def make_server(upstream, port=18081, instance_id="manual", timeout=600):
    upstream = normalize_upstream(upstream)
    server = ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)
    server.upstream = upstream
    server.instance_id = instance_id
    server.upstream_timeout = timeout
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="Server root or /v1 base URL")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--instance-id", default="manual")
    parser.add_argument("--timeout", type=float, default=600, help="Upstream read timeout in seconds")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not 1 <= args.port <= 65535 or args.timeout <= 0:
        parser.error("port must be 1..65535 and timeout must be positive")
    try:
        server = make_server(args.upstream, args.port, args.instance_id, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    LOG.info("listening on 127.0.0.1:%s; forwarding to %s", args.port, server.upstream)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
