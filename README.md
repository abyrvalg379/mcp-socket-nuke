# MCP Socket for Nuke

Local MCP bridge for Foundry Nuke — the Nuke branch of the [mcp-socket](https://github.com/abyrvalg379/mcp-socket) family. A TCP listener runs inside Nuke and speaks the same wire protocol as the Blender bridge (blender-mcp 1.6.x compatible), so any MCP client can drive Nuke through typed tools.

![Smoke 5/5 passing](https://img.shields.io/badge/smoke-5%2F5%20passing-brightgreen)

**Author:** Maksim Kovalev · **Version:** 0.1.0 · **License:** GPL-3.0

*Документация на русском: [README.ru.md](README.ru.md)* · Architecture notes and Nuke 17.1 gotchas: [NUKE.md](NUKE.md)

## How it works

```
MCP client → nuke_mcp.py (stdio) → TCP 127.0.0.1:9897 → mcp_socket_nuke/server.py (inside Nuke)
```

`nuke.executeInMainThread` returns immediately by design (no `WithResult` variant in Nuke), so every command is marshalled by a closure that sends the reply from the main thread while the client-side thread waits on a `threading.Event` (180 s cap). Wire protocol — one JSON document per request, no framing:

```
→ {"type": "ping", "params": {}}
← {"status": "success", "result": {...}}
```

The port auto-walks 9897→9907 for a second Nuke; a `%TEMP%` instance registry with a 10 s heartbeat tracks live pids.

## Tools (11)

| Tool | Purpose |
|------|---------|
| `ping_nuke` | Nuke version/variant, pid, port, scene, frame range, node counts |
| `execute_nuke_code` | Python inside Nuke — `nuke`/`nukescripts` preinjected, stdout/stderr captured, optional `result` variable returned JSON-safely; runs inside one named undo group |
| `undo_agent_session` | walk the undo stack back over the agent's "MCP Socket" groups — stops at the first foreign entry, so the user's own edits are never touched |
| `get_scene_info` | script path, frame range, fps, format, proxy flag, node counts by class, selection |
| `get_hierarchy` | node graph (class, position, disabled/selected flags, inputs), capped at `max_nodes`; `recurse_groups` descends into Group nodes |
| `get_screenshot` | **physical** screen capture via `QScreen.grabWindow` (honest on scaled monitors, unlike `QWidget.grab`); `mode="window"` crops the Nuke window, `"screen"` keeps everything; `focus=true` raises Nuke first |
| `get_console_log` | ring buffer of the whole Nuke session's stdout/stderr — a global tee plus per-execution captures (`last_n` / `filter` / `stream`) |
| `clear_console_log` | clear the ring |
| `list_instances` | live Nuke instances from a `%TEMP%` registry (pid, port, version, scene) |
| `replay_last_session` | re-run the modifying commands of the last recorded session from the JSONL log (read-only steps skipped, replayed steps marked) |
| `get_session_log_path` | path of the newest JSONL session log |

## Agent ergonomics

- **Undo by label walking** — every `execute_nuke_code` opens one named undo group ("MCP Socket: …"); empty (read-only) groups are silently discarded by Nuke, so the stack stays clean. `undo_agent_session` reads the top entry's label (`nuke.Undo.undoDescribe(0)`) and walks back only while it belongs to the agent — a foreign label stops it honestly. In engine mode there is no undo stack and the tool refuses.
- **Session log + replay** — every recorded command appends a JSON line to `%TEMP%\mcp_socket_nuke\sessions\`; a >10 s gap starts a new file, the 30 newest are kept. `replay_last_session` re-runs the modifying commands and marks replayed steps `replay: true`.
- **Menu + window** — an **MCP Socket → Open Window** menu item (idempotent marker block in `menu.py`) opens a status window: version header, start/stop, session log path, console log viewer.
- **Headless testing** — `import nuke` from Nuke's bundled `python.exe` starts an engine without consuming a GUI license seat, so `test_headless.py` (20/20) can run while Nuke is open.

## Install

1. Grab `mcp_socket_nuke_v*.zip` from the [latest release](https://github.com/abyrvalg379/mcp-socket-nuke/releases/latest) and unpack (or clone the repo).
2. Run `python install_nuke_mcp.py` **with Nuke closed** — it copies `mcp_socket_nuke/` and `nuke_mcp.py` into `%USERPROFILE%\.nuke\` and appends idempotent marker blocks to `init.py` (sys.path) and `menu.py` (auto-start + menu item).
3. Start Nuke — the listener auto-starts on `127.0.0.1:9897` (busy port → walks +1…+10 for a second Nuke).

### Connect from any MCP client

Zero dependencies beyond the Python standard library. Example for a JSON-configured client:

```json
{
  "mcpServers": {
    "nuke": {
      "command": "python",
      "args": ["path/to/nuke_mcp.py"]
    }
  }
}
```

`--port 9898` or the `NUKE_MCP_SOCKET_PORT` env var selects a second Nuke instance.

## Smoke test

`tests/socket_smoke.py` is a read-only smoke test for the live bridge — raw TCP on the wire protocol, read-only commands only (`ping`, `get_scene_info`, `get_hierarchy`, `list_instances`, `get_session_log_path`). Never mutates the script, safe to run mid-session:

```
python tests/socket_smoke.py nuke
```

Exit code is 0 when every reachable bridge passes. The same script with no arguments also smokes the Blender (`:9876`), Maya (`:7777`) and Houdini (`:9877`) branches of the family. `test_headless.py` runs against the engine (no GUI) on a throwaway graph and deletes it afterwards.

## Security

localhost-only, no authentication, and `execute_nuke_code` runs arbitrary Python in your Nuke — this is a single-workstation tool for artist+agent workflows, not a service. Do not expose the port.

## Related tools

- [mcp-socket](https://github.com/abyrvalg379/mcp-socket) — the Blender branch of the family (undo checkpoints, console log ring, pipeline FBX presets, offscreen render)
- [mcp-socket-maya](https://github.com/abyrvalg379/mcp-socket-maya) — the Maya branch
- [mcp-socket-houdini](https://github.com/abyrvalg379/mcp-socket-houdini) — the Houdini branch
- [PROKLADKA](https://github.com/abyrvalg379/prokladka) — FBX bridge Blender ↔ Maya ↔ Houdini ↔ UE
