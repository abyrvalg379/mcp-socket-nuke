# -*- coding: utf-8 -*-
"""MCP Socket for Nuke — TCP bridge running inside Nuke (mcp-socket family).

Same two-process design as the Blender/Maya/Houdini branches; wire protocol
is the shared mcp-socket / blender-mcp 1.6.x one, so the stdio server
(nuke_mcp.py) talks to it like any other family member:

  client sends   {"type": "<command>", "params": {...}}
  server replies {"status": "success", "result": ...}
                 {"status": "error",   "message": "..."}

No framing byte — both sides accumulate bytes and try json.loads until the
document parses whole.

Commands
    ping                  versions, variant (Nuke/NukeX/Studio/Indie/NC), pid,
                          port, script, counts
    get_scene_info        script, frame range, fps, format, node counts by class
    get_hierarchy         node graph tree (name, class, inputs, positions,
                          disabled, selected; capped)
    get_screenshot        PHYSICAL screen capture (QScreen.grabWindow) —
                          mode "window" crops the Nuke window, "screen" keeps
                          the full screen. QWidget.grab() would lie on scaled
                          monitors; this is the family-wide canon.
    get_console_log       ring buffer of stdout/stderr (global tee + per-exec)
    clear_console_log
    execute_nuke_code     eval→exec in a fresh namespace with nuke/nukescripts
                          preinjected; captures stdout+stderr, returns the
                          optional ``result`` variable; wrapped in one named
                          undo group ("MCP Socket: ...")
    undo_agent_session    walks the undo stack back over consecutive
                          "MCP Socket" groups (nuke.Undo.undoDescribe) — stops
                          at the first foreign entry (honest, like Houdini)
    list_instances        live Nuke instances from the %TEMP% registry
    replay_last_session   re-run the modifying commands of the last recorded
                          session; read-only steps are skipped, a failed step
                          does not stop the rest
    get_session_log_path  path of the newest JSONL session log

Undo (Nuke flavor — per-call groups + label walk, the Houdini pattern):
every ``execute_nuke_code`` runs inside ``nuke.Undo.begin("MCP Socket: ...")``
/ ``end()``. Empty groups (read-only snippets) are discarded silently by
Nuke, so they never reach the undo stack. ``undo_agent_session`` loops
``nuke.Undo.undoDescribe(0)`` and calls ``nuke.Undo.undo()`` while the top
label starts with "MCP Socket" (max 200 steps), stopping at the first
non-MCP entry — the user's own work is never touched.

Threading: the socket lives on a daemon thread, but Nuke's Python API is
main-thread-only in GUI sessions. GUI mode marshals every command through
``nuke.executeInMainThread``; the reply is sent from inside the main-thread
closure (Maya pattern). ``nuke.executeInMainThread`` "returns immediately"
per its docstring, so the client thread waits on a ``threading.Event`` that
the closure sets — this serializes commands regardless of the return
semantics. In engine mode (``not nuke.GUI``, no event loop) commands run
directly on the socket thread.

Autostart chain: ``~/.nuke/menu.py`` → ``from mcp_socket_nuke import server``
→ ``server.start()`` + the "MCP Socket" menu item (see install script).
"""

from __future__ import annotations

import glob
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from typing import Any, Dict, Optional

import nuke

_TAG = "[MCP_Socket_Nuke]"
VERSION = "0.1.0"

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 9897          # Blender 9876, Maya 7777, Houdini 9877
_PORT_OFFSETS = 10            # busy port → try 9898, 9899, ... (second Nuke)
_ACCEPT_TIMEOUT = 1.0
_RECV_CHUNK = 8192
_CLIENT_IDLE_TIMEOUT = 60.0
_HANDLER_WARN_SECONDS = 30.0
_MAIN_THREAD_TIMEOUT = 180.0  # matches the stdio client's _CALL_TIMEOUT

_SESSION_GAP = 10.0           # session-file border, seconds (family-wide)
_SESSIONS_KEEP = 30           # newest JSONL files kept
_HEARTBEAT_SECONDS = 10.0     # instance-registry heartbeat (daemon thread)
_STALE_SECONDS = 25.0         # registry entry older than this = dead instance
_UNDO_MAX_STEPS = 200         # undo_agent_session hard cap (Houdini parity)
_UNDO_PREFIX = "MCP Socket"

# Stateful globals survive importlib.reload (hot-reload through the bridge
# itself must not orphan the running server / registry / console history):
# on first import globals() lacks them; on reload the dict still holds the
# live objects, so they are picked up instead of reset.
_server = globals().get("_server")          # MCPSocketServer
_thread: Optional[threading.Thread] = globals().get("_thread")
_window = globals().get("_window")          # bridge window (owned by ui.py)
_heartbeat_thread: Optional[threading.Thread] = globals().get("_heartbeat_thread")
_tees: Dict[str, Any] = globals().get("_tees") or {}
_stop_event = globals().get("_stop_event") or threading.Event()

# ── console ring (global tee + per-execution captures) ───────────────────

_LOG_LOCK = globals().get("_LOG_LOCK") or threading.Lock()
_LOG_RING: deque = globals().get("_LOG_RING") or deque(maxlen=500)
# entries: {"ts", "stream", "text"}


def _log_append(stream: str, text: str) -> None:
    if not text:
        return
    with _LOG_LOCK:
        _LOG_RING.append({"ts": time.strftime("%H:%M:%S"),
                          "stream": stream, "text": text[:4000]})


class _Tee:
    """Write-through stream wrapper feeding the console ring.

    Snippet executions bypass it (redirect_stdout inside the handler), so a
    snippet's prints land in the ring exactly once; system messages and
    prints from other tools flow through the tee.
    """

    def __init__(self, original, stream: str):
        self._original = original
        self._stream = stream
        self._partial = ""

    def write(self, text):
        try:
            self._original.write(text)
        except Exception:  # noqa: BLE001 — never break the real stream
            pass
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            _log_append(self._stream, line)
        return len(text)

    def flush(self):
        try:
            self._original.flush()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)


def _install_log_tee() -> None:
    """Idempotent; called on startup and re-checked by the heartbeat
    (self-heal — a reinstall may leave sys.stdout on a stale wrapper)."""
    for name in ("stdout", "stderr"):
        current = getattr(sys, name, None)
        if isinstance(current, _Tee) and current._stream == name:
            continue
        tee = _Tee(current, name)
        _tees[name] = tee
        setattr(sys, name, tee)


def _restore_streams() -> None:
    for name, tee in _tees.items():
        try:
            if getattr(sys, name, None) is tee:
                setattr(sys, name, tee._original)
        except Exception:  # noqa: BLE001
            pass
    _tees.clear()


# ── helpers ───────────────────────────────────────────────────────────────

def _jsonify(value: Any) -> Any:
    """Best-effort JSON-safe conversion of an execution result."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _qt():
    """PySide6 first (Nuke 16+), PySide2 fallback (Nuke 11-15)."""
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        return QtCore, QtGui, QtWidgets
    except ImportError:
        from PySide2 import QtCore, QtGui, QtWidgets  # type: ignore
        return QtCore, QtGui, QtWidgets


def _main_window(QtWidgets):
    """The Nuke main window = the largest visible top-level widget.

    (nuke.mainWindow does not exist in the 17.1 Python API; scanning
    topLevelWidgets is version-proof and picks the full Nuke frame every
    time — it dwarfs any floating panel.)
    """
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    candidates = [w for w in app.topLevelWidgets()
                  if w.isVisible() and w.isWindow()]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w.width() * w.height())


def _nuke_variant() -> str:
    """Nuke / NukeX / Nuke Studio / Nuke Indie / Nuke Non-Commercial."""
    env = nuke.env
    if env.get("studio"):
        return "Nuke Studio"
    if env.get("nukex"):
        return "NukeX"
    if env.get("indie"):
        return "Nuke Indie"
    if env.get("nc"):
        return "Nuke Non-Commercial"
    return "Nuke"


# ── session log (JSONL, family architecture) ─────────────────────────────

_RECORD_SKIP = {"ping", "get_console_log", "clear_console_log",
                "list_instances", "get_session_log_path",
                "replay_last_session"}
_REPLAY_READONLY = {"ping", "get_scene_info", "get_hierarchy",
                    "get_screenshot", "get_console_log", "clear_console_log",
                    "list_instances", "get_session_log_path",
                    "replay_last_session", "undo_agent_session"}

_record_state = globals().get("_record_state") or {"file": None, "last_ts": 0.0}


def _sessions_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "mcp_socket_nuke", "sessions")


def _prune_sessions(keep: int) -> None:
    files = glob.glob(os.path.join(_sessions_dir(), "session_*.jsonl"))
    if len(files) <= keep:
        return
    files.sort(key=os.path.getmtime)
    for path in files[:-keep]:
        try:
            os.remove(path)
        except OSError:
            pass


def _record(command: dict, response: dict, replay: bool = False) -> None:
    ctype = command.get("type")
    if not isinstance(ctype, str) or ctype in _RECORD_SKIP:
        return
    now = time.time()
    st = _record_state
    if st["file"] is None or (now - st["last_ts"]) > _SESSION_GAP:
        os.makedirs(_sessions_dir(), exist_ok=True)
        _prune_sessions(_SESSIONS_KEEP)
        base = os.path.join(_sessions_dir(),
                            "session_%s.jsonl" % time.strftime("%Y%m%d_%H%M%S"))
        path, n = base, 1
        while os.path.exists(path):     # same-second collision guard
            n += 1
            path = base.replace(".jsonl", "_%d.jsonl" % n)
        st["file"] = path
    st["last_ts"] = now
    entry = {"ts": time.strftime("%H:%M:%S"), "type": ctype,
             "params": command.get("params") or {},
             "status": response.get("status")}
    if response.get("status") != "success":
        entry["error"] = response.get("message", "")
    if replay:
        entry["replay"] = True
    try:
        with open(st["file"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("%s session log write failed: %s" % (_TAG, exc))


def _last_session_file() -> Optional[str]:
    files = glob.glob(os.path.join(_sessions_dir(), "session_*.jsonl"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


# ── scene queries ─────────────────────────────────────────────────────────

def _script_name() -> str:
    try:
        return nuke.root().name() or ""
    except Exception:  # noqa: BLE001
        return ""


def _root_knob(name: str):
    try:
        return nuke.root().knob(name).value()
    except Exception:  # noqa: BLE001 — knob missing / wrong type
        return None


def _format_dict(fmt) -> dict:
    out = {}
    try:
        out = {"name": fmt.name() or "", "width": int(fmt.width()),
               "height": int(fmt.height()), "pixel_aspect": float(fmt.pixelAspect())}
    except Exception:  # noqa: BLE001 — defensive: format object surprises
        out = {"repr": repr(fmt)}
    return out


# ── handlers (all run on Nuke's main thread in GUI mode) ──────────────────

def _h_ping(params: dict) -> dict:
    server = _server
    try:
        node_count = len(nuke.allNodes())
    except Exception:  # noqa: BLE001
        node_count = None
    return {
        "app": "nuke",
        "bridge_version": VERSION,
        "nuke_version": nuke.NUKE_VERSION_STRING,
        "variant": _nuke_variant(),
        "gui": bool(nuke.GUI),
        "pid": os.getpid(),
        "port": server.port if server else None,
        "scene": _script_name() or "(untitled)",
        "frames": [_root_knob("first_frame"), _root_knob("last_frame")],
        "nodes": node_count,
        "sessions_dir": _sessions_dir(),
    }


def _h_get_scene_info(params: dict) -> dict:
    nodes = nuke.allNodes()
    counts: Dict[str, int] = {}
    for n in nodes:
        counts[n.Class()] = counts.get(n.Class(), 0) + 1
    selected = [n.name() for n in nodes if n["selected"].value()]
    fmt = _format_dict(_root_knob("format"))
    try:
        root = nuke.root()
        modified = bool(root.knob("modified").value())
    except Exception:  # noqa: BLE001
        modified = None
    return {
        "scene": _script_name() or "(untitled)",
        "modified": modified,
        "variant": _nuke_variant(),
        "frame_range": [_root_knob("first_frame"), _root_knob("last_frame")],
        "fps": _root_knob("fps"),
        "format": fmt,
        "proxy": bool(_root_knob("proxy")),
        "total_nodes": len(nodes),
        "counts": counts,
        "selected": selected[:20],
        "selected_count": len(selected),
    }


def _h_get_hierarchy(params: dict) -> dict:
    max_nodes = int(params.get("max_nodes", 800))
    recurse_groups = bool(params.get("recurse_groups", False))
    if recurse_groups:
        nodes = nuke.allNodes(recurseGroups=True)
    else:
        nodes = nuke.allNodes()
    out, truncated = [], False
    for n in nodes:
        if len(out) >= max_nodes:
            truncated = True
            break
        inputs = []
        try:
            for i in range(n.maximumInputs()):
                dep = n.input(i)
                if dep is not None:
                    inputs.append({"index": i, "node": dep.name()})
        except Exception:  # noqa: BLE001 — unusual node classes
            pass
        entry = {
            "name": n.name(),
            "class": n.Class(),
            "xpos": None,
            "ypos": None,
            "disabled": bool(n.knob("disable").value()) if n.knob("disable") else False,
            "selected": bool(n.knob("selected").value()) if n.knob("selected") else False,
            "inputs": inputs,
        }
        try:
            entry["xpos"] = int(n["xpos"].value())
            entry["ypos"] = int(n["ypos"].value())
        except Exception:  # noqa: BLE001 — nodes without graph position
            pass
        out.append(entry)
    return {"total_nodes": len(nodes), "returned": len(out),
            "truncated": truncated, "nodes": out}


def _h_get_screenshot(params: dict) -> dict:
    """Physical capture of the screen/window — the family canon.

    grabWindow(0) grabs the QScreen's own buffer (physical pixels), unlike
    QWidget.grab() which re-renders offscreen in logical pixels and hides
    DPI artifacts. Mode "window" crops the Nuke window out of that grab.
    """
    _QtCore, QtGui, QtWidgets = _qt()

    mode = params.get("mode", "window")
    if mode not in ("window", "screen"):
        raise ValueError("mode must be 'window' or 'screen'")
    filepath = params.get("filepath") or os.path.join(
        tempfile.gettempdir(), "mcp_socket_nuke",
        "shot_%d.png" % int(time.time() * 1000))
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    main = _main_window(QtWidgets)
    if main is None:
        raise RuntimeError("no visible Nuke main window")

    if params.get("focus"):
        # Raise Nuke before grabbing — otherwise the window rect shows
        # whatever is actually on top. Qt's activateWindow() alone loses to
        # Windows foreground-lock; the ALT-key trick unlocks
        # SetForegroundWindow for our thread (Maya-proven).
        if main.isMinimized():
            main.showMaximized() if main.isMaximized() else main.showNormal()
        main.raise_()
        main.activateWindow()
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.keybd_event(0x12, 0, 0, 0)       # ALT down
            user32.keybd_event(0x12, 0, 2, 0)       # ALT up
            user32.SetForegroundWindow(int(main.winId()))
        except Exception:  # noqa: BLE001 — non-Windows or hardening: Qt-only
            pass
        QtWidgets.QApplication.processEvents()
        main.update()
        time.sleep(1.2)
        QtWidgets.QApplication.processEvents()

    screen = main.screen() or QtWidgets.QApplication.primaryScreen()
    full = screen.grabWindow(0)          # physical pixels of that screen
    dpr = full.devicePixelRatio() or 1.0

    if mode == "window":
        geo = main.frameGeometry()
        origin = screen.geometry().topLeft()
        x = int((geo.x() - origin.x()) * dpr)
        y = int((geo.y() - origin.y()) * dpr)
        w = int(geo.width() * dpr)
        h = int(geo.height() * dpr)
        x, y = max(0, x), max(0, y)
        w = min(w, full.width() - x)
        h = min(h, full.height() - y)
        pix = full.copy(x, y, w, h)
    else:
        pix = full

    max_size = params.get("max_size")
    if max_size and max(pix.width(), pix.height()) > int(max_size):
        pix = pix.scaledToHeight(
            int(pix.height() * int(max_size) / max(pix.width(), pix.height())),
            QtGui.Qt.SmoothTransformation)

    ok = pix.save(filepath, "PNG")
    if not ok:
        raise RuntimeError("failed to save %s" % filepath)
    return {"filepath": filepath, "mode": mode,
            "width": pix.width(), "height": pix.height(), "dpr": dpr}


def _h_get_console_log(params: dict) -> dict:
    last_n = int(params.get("last_n", 50))
    text_filter = str(params.get("filter", "") or "")
    stream = str(params.get("stream", "") or "")
    with _LOG_LOCK:
        entries = list(_LOG_RING)
    if stream:
        entries = [e for e in entries if e["stream"] == stream]
    if text_filter:
        entries = [e for e in entries if text_filter.lower() in e["text"].lower()]
    return {"total": len(entries), "entries": entries[-last_n:]}


def _h_clear_console_log(params: dict) -> str:
    with _LOG_LOCK:
        _LOG_RING.clear()
    return "console log cleared"


def _h_execute_nuke_code(params: dict) -> dict:
    """eval→exec with nuke/nukescripts preinjected; print is the primary
    channel, an optional ``result`` variable is returned JSON-safely.

    Undo: by default the snippet runs inside one named undo group
    ("MCP Socket: execute_nuke_code"). Read-only snippets produce an empty
    group, which Nuke discards silently — the undo stack stays clean.
    ``undo_chunk=False`` runs without a group.
    """
    code = params.get("code")
    if not code:
        raise ValueError("code is required")
    undo_chunk = params.get("undo_chunk", True)

    out_buf, err_buf = io.StringIO(), io.StringIO()
    namespace = {
        "__builtins__": __builtins__,
        "nuke": nuke,
        "os": os,
        "json": json,
        "root": nuke.root(),
    }
    try:
        import nukescripts
        namespace["nukescripts"] = nukescripts
    except Exception:  # noqa: BLE001 — stripped builds
        pass
    ret = None

    undo_open = False
    if undo_chunk and nuke.GUI:
        try:
            nuke.Undo.begin("%s: execute_nuke_code" % _UNDO_PREFIX)
            undo_open = True
        except Exception as exc:  # noqa: BLE001 — undo disabled/nested group
            print("%s undo group unavailable: %s" % (_TAG, exc))

    # eval-then-exec mirrors the family: a successful eval returns
    # immediately (ret may legitimately be None — re-executing the code
    # would duplicate its side effects); only SyntaxError falls to exec,
    # which fills ``result`` if the snippet sets it.
    try:
        import contextlib
        with contextlib.redirect_stdout(out_buf), \
             contextlib.redirect_stderr(err_buf):
            try:
                ret = eval(compile(code, "<mcp_socket>", "eval"), namespace)
            except SyntaxError:
                exec(compile(code, "<mcp_socket>", "exec"), namespace)
                ret = namespace.get("result")
        error_text = ""
    except Exception:  # noqa: BLE001 — traceback is the result
        error_text = traceback.format_exc()
        err_buf.write(error_text)
    finally:
        if undo_open:
            try:
                nuke.Undo.end()
            except Exception:  # noqa: BLE001
                pass

    _log_append("stdout", out_buf.getvalue())
    _log_append("stderr", err_buf.getvalue())

    return {
        "returned": _jsonify(ret),
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "error": bool(error_text),
    }


def _h_undo_agent_session(params: dict) -> dict:
    """Walk the undo stack back over consecutive "MCP Socket" groups.

    ``nuke.Undo.undoDescribe(0)`` names the most recent undo entry; the walk
    stops at the first foreign label — the user's own work is never undone.
    Empty groups (read-only snippets) never reach the stack, so they don't
    stall the walk. Only meaningful in GUI mode (engine has no undo stack).
    """
    if not nuke.GUI:
        return {"undone": 0,
                "message": "engine mode has no undo stack (GUI sessions only)"}
    undone, stopped_at = 0, None
    while undone < _UNDO_MAX_STEPS:
        label = ""
        try:
            label = nuke.Undo.undoDescribe(0) or ""
        except Exception:  # noqa: BLE001
            break
        if not label.startswith(_UNDO_PREFIX):
            stopped_at = label or "(top of stack reached)"
            break
        nuke.Undo.undo()
        undone += 1
    result = {"undone": undone}
    if stopped_at and undone == 0:
        result["message"] = ("no MCP Socket undo groups on top of the stack "
                             "(top entry: %s)" % stopped_at)
    return result


# ── instance registry (multi-Nuke, family architecture) ──────────────────

def _registry_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "mcp_socket_nuke_instances")


def _write_registry() -> None:
    try:
        os.makedirs(_registry_dir(), exist_ok=True)
        data = {"pid": os.getpid(),
                "port": _server.port if _server else None,
                "version": VERSION,
                "scene": _script_name(),
                "ts": time.time()}
        path = os.path.join(_registry_dir(), "pid_%d.json" % os.getpid())
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError as exc:
        print("%s registry write failed: %s" % (_TAG, exc))


def _remove_registry() -> None:
    try:
        os.remove(os.path.join(_registry_dir(), "pid_%d.json" % os.getpid()))
    except OSError:
        pass


def _h_list_instances(params: dict) -> dict:
    fresh, stale = [], 0
    for path in glob.glob(os.path.join(_registry_dir(), "pid_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        entry = {"pid": data.get("pid"), "port": data.get("port"),
                 "version": data.get("version"), "scene": data.get("scene"),
                 "age_seconds": round(time.time() - data.get("ts", 0), 1)}
        if entry["age_seconds"] <= _STALE_SECONDS:
            fresh.append(entry)
        else:
            stale += 1
    fresh.sort(key=lambda e: (e["port"] or 0))
    return {"instances": fresh, "stale_seen": stale}


# ── session replay ────────────────────────────────────────────────────────

def _h_replay_last_session(params: dict) -> dict:
    path = _last_session_file()
    if not path:
        return {"replayed": 0, "message": "no session log found"}
    steps = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    steps.append(json.loads(line))
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot read session log: %s" % exc)

    summary = {"file": path, "total": len(steps), "replayed": 0,
               "skipped_readonly": 0, "skipped_replayed": 0, "failed": []}
    for entry in steps:
        ctype = entry.get("type")
        if ctype in _REPLAY_READONLY:
            summary["skipped_readonly"] += 1
            continue
        if entry.get("replay"):
            summary["skipped_replayed"] += 1
            continue
        command = {"type": ctype, "params": entry.get("params") or {}}
        response = _execute_command(command, replay=True)
        if response.get("status") == "success":
            summary["replayed"] += 1
        else:
            summary["failed"].append({"type": ctype,
                                      "error": response.get("message", "")})
    return summary


def _h_get_session_log_path(params: dict) -> dict:
    return {"dir": _sessions_dir(), "last_file": _last_session_file()}


COMMANDS = {
    "ping": _h_ping,
    "get_scene_info": _h_get_scene_info,
    "get_hierarchy": _h_get_hierarchy,
    "get_screenshot": _h_get_screenshot,
    "get_console_log": _h_get_console_log,
    "clear_console_log": _h_clear_console_log,
    "execute_nuke_code": _h_execute_nuke_code,
    "undo_agent_session": _h_undo_agent_session,
    "list_instances": _h_list_instances,
    "replay_last_session": _h_replay_last_session,
    "get_session_log_path": _h_get_session_log_path,
}


# ── TCP server ────────────────────────────────────────────────────────────

def _try_parse(buffer: bytes):
    """(command, consumed) or (None, 0) — longest JSON prefix wins."""
    try:
        return json.loads(buffer.decode("utf-8")), len(buffer)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    for i in range(1, len(buffer)):
        try:
            return json.loads(buffer[:i].decode("utf-8")), i
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return None, 0


def _execute_command(command: dict, replay: bool = False) -> dict:
    cmd_type = command.get("type")
    params = command.get("params") or {}
    if not isinstance(cmd_type, str):
        return {"status": "error", "message": "Missing 'type' in command"}
    handler = COMMANDS.get(cmd_type)
    if handler is None:
        return {"status": "error",
                "message": "Unknown command type: %r. Known: %s"
                           % (cmd_type, sorted(COMMANDS))}
    started = time.monotonic()
    try:
        result = handler(params) if isinstance(params, dict) else handler()
    except TypeError as exc:
        return {"status": "error",
                "message": "Bad params for %r: %s" % (cmd_type, exc)}
    except Exception as exc:  # noqa: BLE001 — surface as protocol error
        return {"status": "error", "message": "%s: %s" % (type(exc).__name__, exc)}
    elapsed = time.monotonic() - started
    if elapsed > _HANDLER_WARN_SECONDS:
        print("%s slow command %r: %.1fs" % (_TAG, cmd_type, elapsed))
    _record(command, {"status": "success"}, replay)
    return {"status": "success", "result": result}


def _send_reply(client: socket.socket, response: dict, sent: dict,
                lock: threading.Lock) -> None:
    """Send once — the timeout path and the late runner must not interleave."""
    with lock:
        if sent.get("done"):
            return
        sent["done"] = True
    try:
        client.sendall(json.dumps(response).encode("utf-8"))
    except OSError:
        print("%s failed to send reply — client gone" % _TAG)


class MCPSocketServer:
    """Single-port TCP server marshalling commands to Nuke's main thread."""

    def __init__(self, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT):
        self.host = host
        self.port = port
        self.preferred_port = port
        self.running = False
        self.socket: Optional[socket.socket] = None
        self._active_clients = 0
        self._total_commands = 0
        self._lock = threading.Lock()

    def start(self) -> bool:
        last_error: Optional[OSError] = None
        for offset in range(_PORT_OFFSETS + 1):
            candidate = self.preferred_port + offset
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, candidate))
            except OSError as exc:
                sock.close()
                last_error = exc
                continue
            self.port = candidate
            break
        else:
            print("%s failed to bind %s:%d-%d: %s"
                  % (_TAG, self.host, self.preferred_port,
                     self.preferred_port + _PORT_OFFSETS, last_error))
            return False
        sock.listen(1)
        sock.settimeout(_ACCEPT_TIMEOUT)
        self.socket = sock
        self.running = True
        print("%s server started on %s:%d (v%s, %s, gui=%s)"
              % (_TAG, self.host, self.port, VERSION, _nuke_variant(),
                 bool(nuke.GUI)))
        return True

    def stop(self) -> None:
        self.running = False
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
        print("%s server stopped" % _TAG)

    def accept_loop(self) -> None:
        while self.running:
            try:
                client, address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self.running:
                    break
                time.sleep(0.25)
                continue
            with self._lock:
                self._active_clients += 1
            threading.Thread(target=self._client_loop, args=(client,),
                             daemon=True).start()

    def _client_loop(self, client: socket.socket) -> None:
        client.settimeout(_CLIENT_IDLE_TIMEOUT)
        buffer = b""
        try:
            while self.running:
                try:
                    data = client.recv(_RECV_CHUNK)
                except socket.timeout:
                    print("%s client idle, closing" % _TAG)
                    break
                if not data:
                    break
                buffer += data
                command, consumed = _try_parse(buffer)
                if command is None:
                    continue
                buffer = buffer[consumed:]
                with self._lock:
                    self._total_commands += 1
                self._dispatch(client, command)
        except (ConnectionError, OSError) as exc:
            print("%s connection error: %s" % (_TAG, exc))
        finally:
            with self._lock:
                self._active_clients = max(0, self._active_clients - 1)
            try:
                client.close()
            except OSError:
                pass

    def _dispatch(self, client: socket.socket, command: dict) -> None:
        """Run the command on Nuke's main thread (GUI) or inline (engine)."""
        if not nuke.GUI:
            # Engine mode: no event loop to marshal into — run directly.
            # (Headless smoke tests only; never the user's live session.)
            response = _execute_command(command)
            try:
                client.sendall(json.dumps(response).encode("utf-8"))
            except OSError:
                print("%s failed to send reply — client gone" % _TAG)
            return

        sent: dict = {"done": False}
        lock = threading.Lock()

        def runner():
            try:
                response = _execute_command(command)
            except Exception as exc:  # noqa: BLE001 — main-thread marshalling
                response = {"status": "error",
                            "message": "main thread: %s" % exc}
            _send_reply(client, response, sent, lock)

        try:
            # Docstring says "return immediately" — the closure runs whenever
            # Nuke's main loop gets to it. The Event wait below serializes
            # commands regardless of the return semantics (Maya's
            # executeInMainThreadWithResult is NOT in the Nuke API).
            nuke.executeInMainThread(runner)
        except Exception as exc:  # noqa: BLE001 — Nuke busy/shutting down
            _send_reply(client, {"status": "error",
                                 "message": "could not reach main thread: %s"
                                            % exc}, sent, lock)
            return
        if not _stop_event.wait(timeout=_MAIN_THREAD_TIMEOUT):
            _send_reply(client, {"status": "error",
                                 "message": ("main thread did not answer in "
                                             "%.0fs (modal dialog?)" % _MAIN_THREAD_TIMEOUT)},
                        sent, lock)


# ── UI (thin shell over ui.py; lazy import keeps headless runs clean) ─────

def show_window() -> None:
    global _window
    try:
        from mcp_socket_nuke import ui
    except Exception as exc:  # noqa: BLE001
        print("%s cannot open window: %s" % (_TAG, exc))
        return
    _window = ui.show_window(server_module=sys.modules[__name__],
                             current=_window)


# ── heartbeat (daemon thread — no QTimer; instance registry) ─────────────

def _heartbeat_loop() -> None:
    while not _stop_event.wait(timeout=_HEARTBEAT_SECONDS):
        _install_log_tee()      # self-heal: reinstall may leave stale wrappers
        if _server is not None and _server.running:
            _write_registry()


def _start_heartbeat() -> None:
    global _heartbeat_thread
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _stop_event.clear()
    _write_registry()
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    _heartbeat_thread.start()


def _stop_heartbeat() -> None:
    _stop_event.set()


# ── start/stop (called from menu.py — signature preserved) ────────────────

def start(port: int = _DEFAULT_PORT) -> bool:
    global _server, _thread
    if _server is not None and _server.running:
        print("%s already running on %s:%d" % (_TAG, _server.host, _server.port))
        return True
    _server = MCPSocketServer(port=port)
    if not _server.start():
        _server = None
        return False
    _thread = threading.Thread(target=_server.accept_loop, daemon=True)
    _thread.start()
    _install_log_tee()
    _start_heartbeat()
    return True


def stop() -> None:
    global _server, _thread
    if _server:
        _server.stop()
        _server = None
    _thread = None
    _stop_heartbeat()
    _remove_registry()
    _restore_streams()
    print("%s stopped" % _TAG)
