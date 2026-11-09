# Codex usage tool

The `codex_usage` MCP server provides one tool, `usage`, for account usage reads and earned reset management, its background worker runs independently of model requests

## Inputs

| Parameter | Values and behavior |
| --- | --- |
| `action` | `read_remaining_percent`, `read_remaining_resets`, `use_reset`, or `expire_hooks` |
| `hook` | `use_reset_automatically` |
| `limit_of_usages` | Optional positive integer, the total reset limit for the hook |
| `thread_id` | The originating `CODEX_THREAD_ID`, required for a shared MCP server unless its environment identifies the thread |
| `keep_running` | Optional boolean, a new hook defaults to `false`, an existing hook preserves its setting when omitted |

At least one of `action` and `hook` is required, hook options require `hook`, and expiration must be a separate call from hook setup

Example arguments:

```json
{"action":"read_remaining_percent","thread_id":"CURRENT_THREAD_ID"}
{"action":"read_remaining_resets","thread_id":"CURRENT_THREAD_ID"}
{"action":"use_reset","thread_id":"CURRENT_THREAD_ID"}
{"hook":"use_reset_automatically","limit_of_usages":2,"thread_id":"CURRENT_THREAD_ID"}
{"hook":"use_reset_automatically","keep_running":true,"thread_id":"CURRENT_THREAD_ID"}
{"action":"expire_hooks","thread_id":"CURRENT_THREAD_ID"}
```

Usage results retain each quota window and its reset time, missing provider data stays unknown, and reset counts use `availableCount` because the provider can truncate credit detail rows

## Automatic resets

The worker checks account usage every 60 seconds, with a shared polling budget across tool processes, repeated reads can return a cached snapshot with its fetch time

We found no published maximum polling rate for this endpoint, so 60 seconds is a conservative local policy, not a guaranteed provider allowance or a claimed maximum frequency, failures increase the delay and structured retry hints extend it further, a redemption gets one immediate readback

Any reported `codex` quota window at or below 1% remaining can trigger a reset, the worker verifies the owning thread and account before redemption, unrelated quota buckets are reported but do not trigger automatic spending

Successful resets count toward the configured limit, including a manual reset made while that thread owns the hook, an omitted limit allows resets until no credits remain, a successful reset must be followed by observed quota recovery before another automatic reset can be attempted

The reset journal is written before submission and retains the same idempotency key through uncertain responses, a crash between provider acceptance and local confirmation therefore cannot create a fresh automatic attempt, an unresolved attempt from an expired hook blocks new hooks until an explicit `use_reset` reconciles it

## Ownership and cleanup

One ownership file records the thread, worker process identity, account binding, and hook generation, a separate permanent file lock serializes state changes and redemptions, removing ownership leaves that arbitration file in place to avoid races between lock inodes

A competing hook is rejected, stale ownership can be reclaimed only when both the worker is dead and the owning thread is confirmed stopped, an unavailable status remains unknown

The worker is detached from the creating tool call, `Stop` signals a completion check while thread status distinguishes review continuations from finished work, ten seconds of confirmed idle status expires a default hook, the worker also performs that check if lifecycle delivery is unavailable

`keep_running: true` preserves the hook after normal work completion only when the user requests it, `Interrupt` and `SessionEnd` still revoke it, explicit `expire_hooks` removes the caller's ownership and signals its exact worker using a process handle protected against PID reuse

Before enabling the hook, the agent must tell the user that it will stop when work is complete, the MCP description carries that instruction so cleanup does not depend on the agent remembering to call the tool later

## Installation and validation

The runtime requires Linux, Python 3.11 or newer with `venv` and `pip`, `iproute2`, and a local Codex app server, the installer pins the WebSocket dependency and preserves unrelated MCP registrations and lifecycle handlers

The repository installer supports usage-only installation:

```sh
./install.sh --usage-only
```

Codex requires review and trust of the new lifecycle definitions through `/hooks`, a fresh session loads the new MCP registration, existing Codex servers are not restarted by the installer

The direct JSON entry point is also available from the checkout:

```sh
.venv/bin/python tools/usage/server.py --call <<'JSON'
{"action":"read_remaining_percent"}
JSON
```

The direct entry point uses the calling shell's `CODEX_THREAD_ID`, authenticated requests go through the owning Codex server without reading tokens, when several local servers exist the thread must have exactly one loaded owner

Regression checks:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tools/usage/tests -v
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s hooks/tests -p 'test_*.py' -v
```

The integration suite exercises actual MCP and detached worker processes against a local fake Codex server, covering threshold redemption, finite limits, completion cleanup, and interruption without spending real reset credits

Authoritative contracts: [Codex account endpoints](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt), [Codex lifecycle hooks](https://learn.chatgpt.com/docs/hooks), and the [pinned WebSocket client](https://websockets.readthedocs.io/en/15.0.1/reference/sync/client.html)
