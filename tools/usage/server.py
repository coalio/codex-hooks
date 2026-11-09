"""Usage MCP server and the equivalent JSON command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.usage.protocol import UsageError, find_client
from tools.usage.service import PollDeferred, UsageService
from tools.usage.state import Store, thread_key

ACTIONS = ("read_remaining_percent", "read_remaining_resets", "use_reset", "expire_hooks")
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "hook": {"type": "string", "enum": ["use_reset_automatically"]},
        "limit_of_usages": {"type": "integer", "minimum": 1,
                            "description": "Maximum total reset redemptions under this hook, unlimited when omitted"},
        "thread_id": {"type": "string", "description": "The originating CODEX_THREAD_ID, required when the server has no thread-scoped environment"},
        "keep_running": {"type": "boolean", "description": "Only true when the user explicitly requests continuation after work completes, default false for a new hook"},
    },
    "minProperties": 1,
}
TOOL = {"name": "usage", "inputSchema": SCHEMA, "description": (
    "Read remaining Codex usage or earned resets, redeem a reset, or manage one session-owned automatic reset worker. "
    "Provide action (read_remaining_percent, read_remaining_resets, use_reset, expire_hooks) or "
    "hook (use_reset_automatically), at least one is required and validated before any operation. "
    "Read CODEX_THREAD_ID from the shell and supply thread_id, never infer it from the selected UI session. "
    "The worker checks at most once per 60 seconds, with shared caching and backoff, and redeems at <=1% remaining. "
    "Before enabling it, explicitly tell the user that Codex lifecycle cleanup stops it when work completes. "
    "Only set keep_running=true when the user requests that exception. Interrupt and SessionEnd still expire it. "
    "No model polling is needed. A repeated hook call preserves its usage counter and limit. "
    "expire_hooks stops the caller's hook, readings also include hook status and snapshot time."
)}


def validate(arguments):
    if not isinstance(arguments, dict) or set(arguments) - set(SCHEMA["properties"]):
        raise ValueError("Unsupported usage parameters")
    action, hook = arguments.get("action"), arguments.get("hook")
    if not action and not hook:
        raise ValueError("Either action or hook is required")
    if action is not None and action not in ACTIONS:
        raise ValueError("Unsupported usage action")
    if hook is not None and hook != "use_reset_automatically":
        raise ValueError("Unsupported usage hook")
    if action == "expire_hooks" and hook:
        raise ValueError("Expiration and hook setup must be separate calls")
    if not hook and any(key in arguments for key in ("limit_of_usages", "keep_running")):
        raise ValueError("Hook options require hook")
    if "limit_of_usages" in arguments and (type(arguments["limit_of_usages"]) is not int or arguments["limit_of_usages"] < 1):
        raise ValueError("limit_of_usages must be a positive integer")
    if "keep_running" in arguments and type(arguments["keep_running"]) is not bool:
        raise ValueError("keep_running must be a boolean")
    return thread_key(arguments.get("thread_id") or os.environ.get("CODEX_THREAD_ID"))


def invoke(arguments, store=None, client_factory=find_client):
    thread_id = validate(arguments)
    store = store or Store()
    if arguments.get("action") == "expire_hooks":
        return UsageService(store, None, thread_id).expire()
    client = client_factory(thread_id)
    try:
        service = UsageService(store, client, thread_id)
        result = {}
        action = arguments.get("action")
        if action in ("read_remaining_percent", "read_remaining_resets"):
            reading = service.read()
            field = "windows" if action == "read_remaining_percent" else "remaining_resets"
            result = {key: reading[key] for key in (field, "fetched_at", "hook")}
        elif action == "use_reset":
            result = service.use_reset()
        if arguments.get("hook"):
            try:
                result["hook"] = service.start(arguments.get("limit_of_usages"), arguments.get("keep_running"))
            except Exception as error:
                if not action:
                    raise
                result["hook_error"] = (str(error) if isinstance(error, (UsageError, ValueError))
                                        else f"Hook setup failed ({type(error).__name__})")
                return result
            result["poll_interval_seconds"] = 60
            result["cleanup"] = "Codex lifecycle and thread status expire the hook after work completes unless keep_running is true"
        return result
    finally:
        client.close()


def result_for(arguments):
    try:
        value = invoke(arguments)
        return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}
    except Exception as error:
        message = str(error) if isinstance(error, (UsageError, ValueError)) else f"Usage operation failed ({type(error).__name__})"
        value = {"error": message}
        if isinstance(error, PollDeferred):
            value["retry_after_seconds"] = error.retry_after
        return {"isError": True, "content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}


def serve():
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("params", {}), dict):
                raise ValueError("MCP request and parameters must be objects")
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-usage", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": [TOOL]}
            elif method == "ping":
                result = {}
            elif method == "tools/call" and request.get("params", {}).get("name") == "usage":
                result = result_for(request["params"].get("arguments", {}))
            else:
                raise ValueError("Unknown MCP method or tool")
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except (ValueError, TypeError, KeyError):
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid MCP request"}}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call", action="store_true", help="Read one tool argument object from stdin")
    if parser.parse_args().call:
        response = result_for(json.load(sys.stdin))
        print(json.dumps(response["structuredContent"]))
        sys.exit(1 if response.get("isError") else 0)
    serve()
