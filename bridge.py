#!/usr/bin/env python3
"""Translate Codex custom tools to vLLM function tools, preserving native edits."""

import argparse
import copy
import json
import logging
import os
from pathlib import Path
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from loop_guard import ToolLoopError, apply_loop_guard

LOG = logging.getLogger("codex-local-bridge")
VERSION = "1.0.0"


class UpstreamStreamError(Exception):
    """A model stream stopped without a valid terminal completion."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.public_message = message


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep bearer credentials on the explicitly configured upstream.
        return None


UPSTREAM = build_opener(ProxyHandler({}), NoRedirect())


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer normally reverse-resolves its bind address. On macOS that
        # lookup can stall for tens of seconds; a numeric loopback listener does
        # not need DNS to serve requests.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


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

LOCAL_TASK_COMPLETION_INSTRUCTION = """For agent tasks, complete the requested work instead of ending with a progress update or a promise to do more. If you say you will inspect, analyze, edit, or test something, carry it out with the available tools in this turn. End only when the work is complete or you can name a specific blocker."""


def to_upstream(payload, *, loop_guard=False):
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
    if loop_guard:
        apply_loop_guard(payload)
    return payload, custom_names


def _chat_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("message content must be text")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("message content block must be an object")
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text", "refusal"):
            text = part.get("text", "")
            if text:
                parts.append(text)
        elif kind in ("input_image", "image_url"):
            raise ValueError("the configured local model is text-only")
    return "".join(parts)


def _chat_tools(tools):
    result = []
    for tool in tools or []:
        if tool.get("type") != "function":
            continue
        function = {key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool}
        result.append({"type": "function", "function": function})
    return result


def _chat_tool_choice(choice):
    if isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind in ("function", "custom"):
        return {"type": "function", "function": {"name": choice["name"]}}
    if kind == "allowed_tools":
        allowed = choice.get("tools", [])
        if len(allowed) == 1 and allowed[0].get("type") in ("function", "custom"):
            return {"type": "function", "function": {"name": allowed[0]["name"]}}
        return "auto"
    return {"any": "required", "none": "none", "auto": "auto"}.get(kind)


def _responses_input_to_chat(value):
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list):
        raise ValueError("Responses input must be text or a list")
    messages, pending_calls = [], []

    def flush_calls():
        if pending_calls:
            messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_calls)})
            pending_calls.clear()

    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Responses input item must be an object")
        kind = item.get("type")
        if kind == "message":
            flush_calls()
            role = item.get("role", "user")
            if role not in ("system", "developer", "user", "assistant"):
                raise ValueError("unsupported message role")
            messages.append({"role": role, "content": _chat_content(item.get("content"))})
        elif kind == "function_call":
            call_id = item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex)
            pending_calls.append({
                "id": call_id, "type": "function",
                "function": {"name": item["name"], "arguments": item.get("arguments", "{}")},
            })
        elif kind == "function_call_output":
            flush_calls()
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": output})
        elif kind in ("reasoning", "summary"):
            # Reasoning traces are not part of the user-visible chat history.
            continue
        else:
            raise ValueError("unsupported Responses input item: " + str(kind))
    flush_calls()
    return messages


def responses_to_chat(payload, *, loop_guard=False, previous_messages=None):
    """Convert a Codex Responses request to a text-only Chat Completions request."""
    converted, custom_names = to_upstream(payload, loop_guard=loop_guard)
    messages = []
    if previous_messages:
        messages.extend(copy.deepcopy(previous_messages))
    else:
        instructions = converted.get("instructions")
        system_parts = [part for part in (instructions, LOCAL_TASK_COMPLETION_INSTRUCTION)
                        if isinstance(part, str) and part.strip()]
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.extend(_responses_input_to_chat(converted.get("input", [])))
    request = {
        "model": converted.get("model"),
        "messages": messages,
        # Keep the backend streaming when Codex asks for a stream so generated
        # text reaches the CLI immediately instead of after a long completion.
        "stream": bool(converted.get("stream", True)),
    }
    tools = _chat_tools(converted.get("tools"))
    if tools:
        request["tools"] = tools
    choice = _chat_tool_choice(converted.get("tool_choice"))
    if choice is not None:
        request["tool_choice"] = choice
    if "parallel_tool_calls" in converted:
        request["parallel_tool_calls"] = converted["parallel_tool_calls"]
    for key in ("temperature", "top_p", "stop", "seed", "presence_penalty", "frequency_penalty"):
        if key in converted:
            request[key] = converted[key]
    if "max_output_tokens" in converted:
        request["max_tokens"] = converted["max_output_tokens"]
    effort = converted.get("reasoning", {}).get("effort") if isinstance(converted.get("reasoning"), dict) else None
    if effort:
        request["reasoning_effort"] = effort
    text_format = converted.get("text", {}).get("format") if isinstance(converted.get("text"), dict) else None
    if isinstance(text_format, dict):
        if text_format.get("type") == "json_object":
            request["response_format"] = {"type": "json_object"}
        elif text_format.get("type") == "json_schema" and isinstance(text_format.get("json_schema"), dict):
            request["response_format"] = {"type": "json_schema", "json_schema": text_format["json_schema"]}
    return request, custom_names


def chat_to_response(completion, original):
    choices = completion.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        raise ValueError("Chat Completions response has no assistant message")
    choice = choices[0]
    message = choice["message"]
    response_id = completion.get("id") or ("resp_" + uuid.uuid4().hex)
    output = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        output.append({
            "id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    content = message.get("content")
    if content:
        if not isinstance(content, str):
            content = _chat_content(content)
        output.append({
            "id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": content, "annotations": []}],
        })
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function", {})
        output.append({
            "id": "fc_" + uuid.uuid4().hex, "type": "function_call", "status": "completed",
            "call_id": tool_call.get("id") or ("call_" + uuid.uuid4().hex),
            "name": function.get("name", ""), "arguments": function.get("arguments", "{}"),
        })
    finish_reason = choice.get("finish_reason")
    if finish_reason is None:
        raise UpstreamStreamError(
            "missing_finish_reason", "The local model response did not include a finish reason."
        )
    incomplete_reason = {"length": "max_output_tokens", "content_filter": "content_filter"}.get(finish_reason)
    incomplete = incomplete_reason is not None
    usage = completion.get("usage") or {}
    response = {
        "id": response_id, "object": "response", "created_at": completion.get("created", int(time.time())),
        "status": "incomplete" if incomplete else "completed", "error": None,
        "incomplete_details": {"reason": incomplete_reason} if incomplete else None,
        "instructions": original.get("instructions"), "model": completion.get("model", original.get("model")),
        "output": output, "parallel_tool_calls": original.get("parallel_tool_calls", True),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }
    return response


def response_events(response):
    """Build the Responses SSE event sequence for a completed Chat completion."""
    current = copy.deepcopy(response)
    current["status"] = "in_progress"
    current["output"] = []
    yield {"type": "response.created", "response": current}
    yield {"type": "response.in_progress", "response": current}
    for index, item in enumerate(response["output"]):
        base = {"response_id": response["id"], "output_index": index}
        if item["type"] == "reasoning":
            partial = {**item, "status": "in_progress", "summary": []}
            yield {**base, "type": "response.output_item.added", "item": partial}
            for summary_index, part in enumerate(item.get("summary", [])):
                part_base = {**base, "item_id": item["id"], "summary_index": summary_index}
                empty_part = {**part, "text": ""}
                yield {**part_base, "type": "response.reasoning_summary_part.added", "part": empty_part}
                if part.get("text"):
                    yield {**part_base, "type": "response.reasoning_summary_text.delta", "delta": part["text"]}
                yield {**part_base, "type": "response.reasoning_summary_text.done", "text": part.get("text", "")}
                yield {**part_base, "type": "response.reasoning_summary_part.done", "part": part}
            yield {**base, "type": "response.output_item.done", "item": item}
        elif item["type"] == "message":
            partial = {**item, "status": "in_progress", "content": []}
            yield {**base, "type": "response.output_item.added", "item": partial}
            for content_index, part in enumerate(item["content"]):
                part_base = {**base, "item_id": item["id"], "content_index": content_index}
                empty_part = {**part, "text": ""}
                yield {**part_base, "type": "response.content_part.added", "part": empty_part}
                if part.get("text"):
                    yield {**part_base, "type": "response.output_text.delta", "delta": part["text"]}
                yield {**part_base, "type": "response.output_text.done", "text": part.get("text", "")}
                yield {**part_base, "type": "response.content_part.done", "part": part}
            yield {**base, "type": "response.output_item.done", "item": item}
        else:
            partial = {**item, "status": "in_progress", "arguments": ""}
            yield {**base, "type": "response.output_item.added", "item": partial}
            arguments = item.get("arguments", "")
            if arguments:
                yield {**base, "type": "response.function_call_arguments.delta", "item_id": item["id"], "delta": arguments}
            yield {**base, "type": "response.function_call_arguments.done", "item_id": item["id"], "arguments": arguments}
            yield {**base, "type": "response.output_item.done", "item": item}
    final = copy.deepcopy(response)
    yield {"type": "response.incomplete" if final["status"] == "incomplete" else "response.completed", "response": final}


class ChatStreamTranslator:
    """Translate streamed Chat Completions chunks into Responses SSE events."""

    def __init__(self, original):
        self.original = original
        self.response = {
            "id": "resp_" + uuid.uuid4().hex, "object": "response",
            "created_at": int(time.time()), "status": "in_progress", "error": None,
            "incomplete_details": None, "instructions": original.get("instructions"),
            "model": original.get("model"), "output": [],
            "parallel_tool_calls": original.get("parallel_tool_calls", True), "usage": None,
        }
        self.started = False
        self.next_output_index = 0
        self.message = None
        self.message_text = ""
        self.message_index = None
        self.tool_calls = {}
        self.finish_reason = None
        self.usage = {}
        self.reasoning_text = ""
        self.reasoning_item = None
        self.reasoning_index = None

    def _start(self, chunk):
        if self.started:
            return []
        self.started = True
        if chunk.get("model"):
            self.response["model"] = chunk["model"]
        if chunk.get("created"):
            self.response["created_at"] = chunk["created"]
        snapshot = copy.deepcopy(self.response)
        return [
            {"type": "response.created", "response": snapshot},
            {"type": "response.in_progress", "response": copy.deepcopy(snapshot)},
        ]

    def _add_message(self):
        if self.message is not None:
            return []
        self.message_index = self.next_output_index
        self.next_output_index += 1
        self.message = {
            "id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "in_progress",
            "role": "assistant", "content": [],
        }
        self.response["output"].append(self.message)
        part = {"type": "output_text", "text": "", "annotations": []}
        self.message["content"].append(part)
        base = {"response_id": self.response["id"], "output_index": self.message_index}
        return [
            {**base, "type": "response.output_item.added", "item": copy.deepcopy(self.message)},
            {**base, "item_id": self.message["id"], "content_index": 0,
             "type": "response.content_part.added", "part": copy.deepcopy(part)},
        ]

    def _add_reasoning(self):
        if self.reasoning_item is not None:
            return []
        self.reasoning_index = self.next_output_index
        self.next_output_index += 1
        self.reasoning_item = {
            "id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "status": "in_progress",
            "summary": [],
        }
        self.response["output"].append(self.reasoning_item)
        return [
            {"type": "response.output_item.added", "response_id": self.response["id"],
             "output_index": self.reasoning_index, "item": copy.deepcopy(self.reasoning_item)},
            {"type": "response.reasoning_summary_part.added", "response_id": self.response["id"],
             "item_id": self.reasoning_item["id"], "output_index": self.reasoning_index,
             "summary_index": 0, "part": {"type": "summary_text", "text": ""}},
        ]

    def _add_tool(self, index, *, force=False):
        state = self.tool_calls[index]
        if state.get("item") is not None or (not force and not state["name"]):
            return []
        if not state["name"]:
            raise ValueError("Chat Completions stream ended with an unnamed tool call")
        state["call_id"] = state.get("call_id") or ("call_" + uuid.uuid4().hex)
        state["output_index"] = self.next_output_index
        self.next_output_index += 1
        state["item"] = {
            "id": "fc_" + uuid.uuid4().hex, "type": "function_call", "status": "in_progress",
            "call_id": state["call_id"], "name": state["name"], "arguments": "",
        }
        self.response["output"].append(state["item"])
        base = {"response_id": self.response["id"], "output_index": state["output_index"]}
        return [{**base, "type": "response.output_item.added", "item": copy.deepcopy(state["item"])}]

    def process(self, chunk):
        events = self._start(chunk)
        if chunk.get("model"):
            self.response["model"] = chunk["model"]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                events.extend(self._add_reasoning())
                self.reasoning_text += reasoning
                # Codex renders Responses reasoning-summary events in its activity view.
                events.append({
                    "type": "response.reasoning_summary_text.delta", "response_id": self.response["id"],
                    "output_index": self.reasoning_index, "item_id": self.reasoning_item["id"],
                    "summary_index": 0, "delta": reasoning,
                })
            content = delta.get("content")
            if isinstance(content, str) and content:
                events.extend(self._add_message())
                self.message_text += content
                self.message["content"][0]["text"] += content
                events.append({
                    "type": "response.output_text.delta", "response_id": self.response["id"],
                    "output_index": self.message_index, "item_id": self.message["id"],
                    "content_index": 0, "delta": content,
                })
            for tool_delta in delta.get("tool_calls") or []:
                index = tool_delta.get("index", 0)
                state = self.tool_calls.setdefault(index, {
                    "call_id": None, "name": "", "arguments": "", "sent_arguments": 0,
                    "item": None, "output_index": None,
                })
                if tool_delta.get("id"):
                    state["call_id"] = tool_delta["id"]
                function = tool_delta.get("function") or {}
                if isinstance(function.get("name"), str):
                    state["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    state["arguments"] += function["arguments"]
                events.extend(self._add_tool(index))
                if state.get("item") is not None and state["sent_arguments"] < len(state["arguments"]):
                    delta_text = state["arguments"][state["sent_arguments"]:]
                    state["sent_arguments"] = len(state["arguments"])
                    events.append({
                        "type": "response.function_call_arguments.delta",
                        "response_id": self.response["id"], "output_index": state["output_index"],
                        "item_id": state["item"]["id"], "delta": delta_text,
                    })
                    state["item"]["arguments"] += delta_text
            if choice.get("finish_reason") is not None:
                self.finish_reason = choice["finish_reason"]
        return events

    def process_completion(self, completion):
        """Feed a buffered Chat Completion through the same Responses event path."""
        choices = completion.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise ValueError("Chat Completions response has no assistant message")
        choice = choices[0]
        message = choice["message"]
        delta = {}
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            delta["reasoning_content"] = reasoning
        content = message.get("content")
        if content is not None:
            delta["content"] = content if isinstance(content, str) else _chat_content(content)
        tool_calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            tool_calls.append({
                "index": index, "id": call.get("id"), "type": "function",
                "function": {"name": function.get("name", ""),
                             "arguments": function.get("arguments", "{}")},
            })
        if tool_calls:
            delta["tool_calls"] = tool_calls
        return self.process({
            "model": completion.get("model"), "created": completion.get("created"),
            "usage": completion.get("usage"),
            "choices": [{"index": 0, "delta": delta, "finish_reason": choice.get("finish_reason")}],
        })

    def finish(self):
        if self.finish_reason is None:
            raise UpstreamStreamError(
                "missing_finish_reason", "The local model stream ended without a finish reason."
            )
        events = []
        if not self.started:
            events.extend(self._start({}))
        if self.reasoning_item is not None:
            part = {"type": "summary_text", "text": self.reasoning_text}
            self.reasoning_item["summary"] = [part]
            self.reasoning_item["status"] = "completed"
            base = {
                "response_id": self.response["id"], "output_index": self.reasoning_index,
                "item_id": self.reasoning_item["id"], "summary_index": 0,
            }
            events.extend([
                {**base, "type": "response.reasoning_summary_text.done", "text": self.reasoning_text},
                {**base, "type": "response.reasoning_summary_part.done", "part": copy.deepcopy(part)},
                {"type": "response.output_item.done", "response_id": self.response["id"],
                 "output_index": self.reasoning_index, "item": copy.deepcopy(self.reasoning_item)},
            ])
        if self.message is not None:
            part = self.message["content"][0]
            base = {
                "response_id": self.response["id"], "output_index": self.message_index,
                "item_id": self.message["id"], "content_index": 0,
            }
            self.message["status"] = "completed"
            events.extend([
                {**base, "type": "response.output_text.done", "text": self.message_text},
                {**base, "type": "response.content_part.done", "part": copy.deepcopy(part)},
                {"type": "response.output_item.done", "response_id": self.response["id"],
                 "output_index": self.message_index, "item": copy.deepcopy(self.message)},
            ])
        for index, state in self.tool_calls.items():
            events.extend(self._add_tool(index, force=True))
            state["item"]["arguments"] = state["arguments"]
            base = {
                "response_id": self.response["id"], "output_index": state["output_index"],
                "item_id": state["item"]["id"],
            }
            state["item"]["status"] = "completed"
            events.extend([
                {**base, "type": "response.function_call_arguments.done", "arguments": state["arguments"]},
                {"type": "response.output_item.done", "response_id": self.response["id"],
                 "output_index": state["output_index"], "item": copy.deepcopy(state["item"])},
            ])
        usage = self.usage
        self.response["usage"] = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        incomplete_reason = {"length": "max_output_tokens", "content_filter": "content_filter"}.get(
            self.finish_reason
        )
        incomplete = incomplete_reason is not None
        self.response["status"] = "incomplete" if incomplete else "completed"
        self.response["incomplete_details"] = {"reason": incomplete_reason} if incomplete else None
        events.append({
            "type": "response.incomplete" if incomplete else "response.completed",
            "response": copy.deepcopy(self.response),
        })
        return events

    def chat_message(self):
        message = {"role": "assistant", "content": self.message_text or None}
        if self.tool_calls:
            message["tool_calls"] = [{
                "id": state.get("call_id") or ("call_" + uuid.uuid4().hex), "type": "function",
                "function": {"name": state["name"], "arguments": state["arguments"] or "{}"},
            } for state in self.tool_calls.values()]
        return message


def resolve_dashboard_model(directory, default_port, bridge_port):
    """Resolve the dashboard's selected profile to its live Chat Completions API."""
    root = Path(directory)
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    catalog = json.loads((root / "models.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(catalog, list):
        raise ValueError("dashboard model state is unavailable")
    by_id = {str(model.get("id")): model for model in catalog if isinstance(model, dict) and model.get("id")}
    selected = str(state.get("selected_model") or "")
    if selected and selected in by_id:
        model = by_id[selected]
    else:
        active = [by_id[model_id] for model_id in state.get("parallel_models", [])
                  if str(model_id) in by_id]
        if len(active) != 1:
            raise ValueError("select exactly one running model in the dashboard")
        model = active[0]
    try:
        port = int(model.get("api_port", default_port))
    except (TypeError, ValueError):
        port = int(default_port)
    if not 1 <= port <= 65535 or port == int(bridge_port):
        raise ValueError("dashboard model API port conflicts with the Codex endpoint")
    root_url = f"http://127.0.0.1:{port}"
    request = Request(root_url + "/v1/models", headers={"Accept": "application/json"})
    with UPSTREAM.open(request, timeout=3) as response:
        payload = json.loads(response.read(200_000).decode("utf-8", "replace"))
    models = payload.get("data", []) if isinstance(payload, dict) else []
    model_id = models[0].get("id") if models and isinstance(models[0], dict) else None
    if not model_id:
        raise ValueError("the dashboard model API is not ready")
    return {"url": root_url, "model_id": str(model_id), "dashboard_model_id": str(model["id"]), "port": port}


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
        sequence = 0
        response_stream = None
        translator = None
        try:
            if self.path == "/health":
                self.send_body(200, json.dumps({
                    "status": "ok", "service": "codex-local-bridge", "version": VERSION,
                    "instance_id": self.server.instance_id, "pid": os.getpid(),
                    "loop_guard": self.server.loop_guard, "upstream_api": self.server.upstream_api,
                }).encode())
                return
            if not self.path.startswith("/v1/"):
                self.send_body(404, b'{"error":{"message":"Unknown route"}}')
                return
            route = self.path.split("?", 1)[0]
            if (self.command == "GET" and route == "/v1/models"
                    and self.server.model_panel_dir):
                self.send_body(200, json.dumps({
                    "object": "list",
                    "data": [{"id": self.server.model_alias, "object": "model", "owned_by": "local-dashboard"}],
                }).encode("utf-8"))
                return
            names = set()
            data = None
            response_request = False
            original_payload = None
            chat_messages = None
            stream_requested = False
            upstream_path = self.path
            target_upstream = self.server.upstream
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
                if route == "/v1/responses" and self.server.upstream_api == "chat-completions":
                    try:
                        original_payload = json.loads(data)
                        if not isinstance(original_payload, dict):
                            raise ValueError("expected an object")
                        stream_requested = original_payload.get("stream", True)
                        previous_id = original_payload.get("previous_response_id")
                        previous_messages = self.server.response_history.get(previous_id) if previous_id else None
                        payload, names = responses_to_chat(
                            original_payload, loop_guard=self.server.loop_guard,
                            previous_messages=previous_messages,
                        )
                    except ToolLoopError as exc:
                        LOG.info("stopped an unchanged tool-call loop")
                        self.send_body(400, json.dumps({"error": {
                            "type": "invalid_request_error", "code": "local_tool_loop",
                            "message": str(exc)}}).encode("utf-8"))
                        return
                    except (ValueError, KeyError, TypeError, AttributeError):
                        self.send_body(400, b'{"error":{"message":"Invalid Responses request"}}')
                        return
                    chat_messages = copy.deepcopy(payload["messages"])
                    if self.server.model_panel_dir:
                        try:
                            active = resolve_dashboard_model(
                                self.server.model_panel_dir, self.server.default_model_port,
                                self.server.server_port,
                            )
                        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, URLError) as exc:
                            LOG.info("dashboard model is not ready: %s", type(exc).__name__)
                            self.send_body(503, json.dumps({"error": {
                                "type": "server_error",
                                "message": "No ready dashboard model is available at the Codex endpoint.",
                            }}).encode("utf-8"))
                            return
                        payload["model"] = active["model_id"]
                        target_upstream = active["url"]
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    upstream_path = "/v1/chat/completions"
                    response_request = True
                elif route == "/v1/responses":
                    try:
                        payload = json.loads(data)
                        if not isinstance(payload, dict):
                            raise ValueError("expected an object")
                        payload, names = to_upstream(payload, loop_guard=self.server.loop_guard)
                    except ToolLoopError as exc:
                        LOG.info("stopped an unchanged tool-call loop")
                        self.send_body(400, json.dumps({"error": {
                            "type": "invalid_request_error", "code": "local_tool_loop",
                            "message": str(exc)}}).encode("utf-8"))
                        return
                    except (ValueError, KeyError, TypeError, AttributeError):
                        self.send_body(400, b'{"error":{"message":"Invalid Responses request"}}')
                        return
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            accept = (
                "text/event-stream" if response_request and stream_requested else
                "application/json" if response_request else
                self.headers.get("Accept", "application/json")
            )
            headers = {"Content-Type": "application/json", "Accept": accept}
            if "Authorization" in self.headers:
                headers["Authorization"] = self.headers["Authorization"]
            req = Request(target_upstream + upstream_path, data=data, headers=headers, method=self.command)
            if response_request and stream_requested:
                response_stream = ChatStreamTranslator(original_payload)
                translator = EventTranslator(names)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                streaming = True
                for event in response_stream.process({}):
                    for translated in translator.process(event):
                        translated["sequence_number"] = sequence
                        sequence += 1
                        self.send_event(translated)
            with UPSTREAM.open(req, timeout=self.server.upstream_timeout) as upstream:
                content_type = upstream.headers.get("Content-Type", "application/json")
                if "text/event-stream" not in content_type:
                    body = upstream.read()
                    if response_request and upstream.status < 400:
                        completion = json.loads(body)
                        if isinstance(completion, dict) and completion.get("error"):
                            upstream_error = completion["error"]
                            detail = upstream_error.get("message") if isinstance(upstream_error, dict) else None
                            if not isinstance(detail, str) or not detail.strip():
                                detail = "The local model reported an error before completing the response."
                            raise UpstreamStreamError("upstream_error_response", " ".join(detail.split())[:500])
                        if stream_requested:
                            for event in response_stream.process_completion(completion):
                                for translated in translator.process(event):
                                    translated["sequence_number"] = sequence
                                    sequence += 1
                                    self.send_event(translated)
                            final_events = response_stream.finish()
                            LOG.info("model response ended: status=%s finish_reason=%s output_tokens=%s",
                                     response_stream.response["status"], response_stream.finish_reason,
                                     (response_stream.usage or {}).get("completion_tokens", 0))
                            for event in final_events:
                                for translated in translator.process(event):
                                    translated["sequence_number"] = sequence
                                    sequence += 1
                                    self.send_event(translated)
                            self.server.response_history[response_stream.response["id"]] = chat_messages + [
                                response_stream.chat_message()
                            ]
                            if len(self.server.response_history) > 128:
                                self.server.response_history.pop(next(iter(self.server.response_history)))
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                            return
                        response = chat_to_response(completion, original_payload)
                        history_message = copy.deepcopy(completion["choices"][0]["message"])
                        history_message.pop("reasoning_content", None)
                        self.server.response_history[response["id"]] = chat_messages + [
                            history_message
                        ]
                        if len(self.server.response_history) > 128:
                            self.server.response_history.pop(next(iter(self.server.response_history)))
                        response = restore_response(response, names)
                        self.send_body(upstream.status, json.dumps(response, ensure_ascii=False).encode("utf-8"))
                        return
                    if response_request and stream_requested:
                        self.send_event({
                            "type": "error", "code": "local_model_http_error",
                            "message": f"The local model returned HTTP {upstream.status}.", "param": None,
                            "sequence_number": sequence,
                        })
                        return
                    if names and "json" in content_type:
                        body = json.dumps(restore_response(json.loads(body), names), ensure_ascii=False).encode("utf-8")
                    self.send_body(upstream.status, body, content_type)
                    return
                if response_request:
                    response_stream = response_stream or ChatStreamTranslator(original_payload)
                    translator = translator or EventTranslator(names)
                    if stream_requested:
                        if not streaming:
                            self.send_response(upstream.status)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            self.close_connection = True
                            streaming = True
                            for event in response_stream.process({}):
                                for translated in translator.process(event):
                                    translated["sequence_number"] = sequence
                                    sequence += 1
                                    self.send_event(translated)
                    saw_done = False
                    for raw in sse_data(upstream):
                        if raw == "[DONE]":
                            saw_done = True
                            continue
                        chunk = json.loads(raw)
                        if isinstance(chunk, dict) and chunk.get("error"):
                            raise UpstreamStreamError(
                                "upstream_error_event", "The local model reported an error during generation."
                            )
                        for event in response_stream.process(chunk):
                            if stream_requested:
                                for translated in translator.process(event):
                                    translated["sequence_number"] = sequence
                                    sequence += 1
                                    self.send_event(translated)
                    if not saw_done:
                        raise UpstreamStreamError(
                            "missing_done_marker", "The local model stream closed before its final marker."
                        )
                    final_events = response_stream.finish()
                    LOG.info("model stream ended: status=%s finish_reason=%s output_tokens=%s",
                             response_stream.response["status"], response_stream.finish_reason,
                             (response_stream.usage or {}).get("completion_tokens", 0))
                    for event in final_events:
                        if stream_requested:
                            for translated in translator.process(event):
                                translated["sequence_number"] = sequence
                                sequence += 1
                                self.send_event(translated)
                    self.server.response_history[response_stream.response["id"]] = chat_messages + [
                        response_stream.chat_message()
                    ]
                    if len(self.server.response_history) > 128:
                        self.server.response_history.pop(next(iter(self.server.response_history)))
                    if stream_requested:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    else:
                        response = restore_response(response_stream.response, names)
                        self.send_body(upstream.status, json.dumps(response, ensure_ascii=False).encode("utf-8"))
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
                body = exc.read()
                content_type = exc.headers.get("Content-Type", "application/json")
            if streaming:
                message = f"The local model returned HTTP {exc.code}."
                try:
                    payload = json.loads(body)
                    upstream_error = payload.get("error") if isinstance(payload, dict) else None
                    detail = upstream_error.get("message") if isinstance(upstream_error, dict) else None
                    if isinstance(detail, str) and detail.strip():
                        message = " ".join(detail.split())[:500]
                except (ValueError, TypeError, AttributeError):
                    pass
                try:
                    self.send_event({
                        "type": "error", "code": "local_model_http_error", "message": message,
                        "param": None, "sequence_number": sequence,
                    })
                except (BrokenPipeError, ConnectionResetError):
                    LOG.info("client disconnected after upstream HTTP error")
            else:
                self.send_body(exc.code, body, content_type)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("client disconnected")
        except UpstreamStreamError as exc:
            LOG.error("model stream incomplete: %s", exc.code)
            error = {"type": exc.code, "code": exc.code, "message": exc.public_message}
            try:
                if streaming:
                    self.send_event({
                        "type": "error", "code": exc.code, "message": exc.public_message, "param": None,
                        "sequence_number": sequence,
                    })
                else:
                    self.send_body(502, json.dumps({"error": error}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (ValueError, KeyError, TypeError, URLError, TimeoutError, OSError) as exc:
            LOG.error("bridge request failed: %s", type(exc).__name__)
            message = "Local tool adapter could not complete the request: " + type(exc).__name__
            error = {"type": "local_bridge_error", "code": "local_bridge_error", "message": message}
            try:
                if streaming:
                    self.send_event({"type": "error", "code": "local_bridge_error",
                                     "message": message, "param": None, "sequence_number": sequence})
                else:
                    self.send_body(502, json.dumps({"error": error}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_event(self, event):
        frame = "event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()


def make_server(upstream, port=18081, instance_id="manual", timeout=600, *, loop_guard=False,
                upstream_api="responses", model_panel_dir=None, model_alias="gpu_server_local",
                default_model_port=8081):
    if upstream_api not in ("responses", "chat-completions"):
        raise ValueError("upstream API must be responses or chat-completions")
    if not 1 <= int(default_model_port) <= 65535:
        raise ValueError("default model port must be 1..65535")
    upstream = normalize_upstream(upstream)
    server = LoopbackServer(("127.0.0.1", port), BridgeHandler)
    server.upstream = upstream
    server.instance_id = instance_id
    server.upstream_timeout = timeout
    server.loop_guard = loop_guard
    server.upstream_api = upstream_api
    server.model_panel_dir = str(Path(model_panel_dir).expanduser().resolve()) if model_panel_dir else None
    server.model_alias = model_alias
    server.default_model_port = int(default_model_port)
    server.response_history = {}
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="Server root or /v1 base URL")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--instance-id", default="manual")
    parser.add_argument("--timeout", type=float, default=600, help="Upstream read timeout in seconds")
    parser.add_argument("--upstream-api", choices=("responses", "chat-completions"), default="responses",
                        help="Upstream API protocol (Codex-facing API is always Responses)")
    parser.add_argument("--model-panel-dir", help="Resolve the selected backend from dashboard state.json and models.json")
    parser.add_argument("--model-alias", default="gpu_server_local", help="Stable model id exposed to Codex")
    parser.add_argument("--default-model-port", type=int, default=8081,
                        help="Dashboard's fallback API port for profiles without api_port")
    parser.add_argument("--loop-guard", action="store_true", help="Detect unchanged repeated tool calls")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not 1 <= args.port <= 65535 or args.timeout <= 0:
        parser.error("port must be 1..65535 and timeout must be positive")
    try:
        server = make_server(args.upstream, args.port, args.instance_id, args.timeout,
                             loop_guard=args.loop_guard, upstream_api=args.upstream_api,
                             model_panel_dir=args.model_panel_dir, model_alias=args.model_alias,
                             default_model_port=args.default_model_port)
    except ValueError as exc:
        parser.error(str(exc))
    LOG.info("listening on 127.0.0.1:%s; forwarding to %s (%s%s)",
             args.port, server.upstream, server.upstream_api,
             ", dashboard-routed" if server.model_panel_dir else "")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
