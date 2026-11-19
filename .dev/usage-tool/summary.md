# Usage tool handoff

The request is a global Codex tool for remaining usage, remaining earned resets, explicit redemption, and session-owned automatic reset hooks, the user chose the existing codex-hooks repository and approved work without an Asana task

We used branch `feat/usage-tool`, beginning at `28a9ca8`, the existing checkout was clean

## Decisions and behavior

The approved interface includes optional `thread_id` and `keep_running` alongside the requested action, hook, and usage limit, thread routing uses a unique loaded owner on the local Codex app server

The automatic worker checks reported Codex quota windows at or below 1% remaining, a limit counts successful resets and an omitted limit permits operation until available credits are exhausted, manual resets made under an active hook count toward its cap

We found no published polling allowance for the account endpoint, the implementation uses a shared 60-second interval with increasing failure delays and structured retry hints, fresh setup data can serve the first check and a redemption gets an immediate readback

The worker, ownership record, operation lock, and durable reset journal run without model calls, stale ownership requires a dead worker and a stopped thread, recovery after an original server outage additionally checks for a resumed owner on other live local servers

Lifecycle handlers signal completion, interruption, and session end, a default worker expires after ten seconds of confirmed idle status to allow review continuations, `keep_running` preserves normal completion only, interruption and session end still revoke authority, cancellation uses a separate record so a late Stop cannot overwrite it

## Problems and resolutions

The installation's default daemon socket was absent, so discovery uses the user's actual local Codex listeners and verifies thread ownership through the app-server protocol

The initial loaded-thread request received JSON-RPC error `-32600` because it omitted the required empty parameter object, the client now supplies parameter objects and follows pagination

The first process tests exposed detached-child resource warnings, a daemon waiter now reaps the launched worker without tying its lifetime to the tool call

During self-review, an idle timer was found to be reset by the preliminary event check, the event check now handles only immediate revocations and the integration suite verifies actual process exit after idle

Cancellation, uncertain responses, and unchanged depleted readbacks were checked at the redemption boundary, pending attempts retain their key, counts commit with a receipt, and another automatic reset requires observed quota recovery

The native tool's schema renderer initially reduced the root `anyOf` contract to generic argument objects, the schema now exposes the five optional fields directly while runtime validation enforces the required action-or-hook rule before any operation

## Validation

The usage suite passes 36 tests, including real MCP and worker subprocesses against a local fake Codex server, the existing review-hook suite passes 46 tests

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tools/usage/tests -q
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s hooks/tests -p 'test_*.py' -q
git diff --check
```

A live read through the owning server confirmed a weekly quota and zero available resets, no real reset was redeemed and no live automatic reset worker was enabled

Global installation completed through `./install.sh --usage-only`, all nine installed Python files match their source, the installed MCP handshake, discovery, and both live read actions passed, the final read returned 43% remaining and zero resets

The replaced configuration was backed up, comparison against that backup confirms unrelated configuration and all existing review handlers were preserved

After explicit user approval, the three exact cleanup definitions were trusted through Codex's configuration API, live readback reports each definition as trusted and enabled for both configuration discovery and this loaded thread, previous trust entries and unrelated settings were preserved

The configuration reload also exposed the native `usage` MCP tool in the current session, both native read actions passed with 42% remaining and zero resets, no restart was required

## Commits and remaining work

- `ef9cfd2`, account quota reads and local server discovery
- `5602829`, session ownership, background policy, reset journal, and lifecycle processing
- `168c939`, MCP entry point, installation, lifecycle registration, documentation, and process-level validation

No operator activation remains pending, Codex's native MCP inventory confirms the refreshed five-field schema and a native call passed after reload, no GitHub push, release, or external communication is part of this request, no Asana time was logged because this work was explicitly approved without an Asana task

The implementation follows the current [Codex app-server contract](https://learn.chatgpt.com/docs/app-server) and [lifecycle hook contract](https://learn.chatgpt.com/docs/hooks), with the [pinned WebSocket client API](https://websockets.readthedocs.io/en/15.0.1/reference/sync/client.html)
