# -*- coding: utf-8 -*-
"""Bridge window for MCP Socket Nuke (family UI: Maya window / Blender panel).

Status line, Start/Stop, one-click rollback of the agent session, session
log helpers and a live console tail. Owned by server.show_window(); a fresh
window replaces the old one on every call.
"""

from __future__ import annotations

import json
import os
import tempfile
import time


def show_window(server_module, current=None):
    """Build (or rebuild) the bridge window. Returns the new widget."""
    _QtCore, QtGui, QtWidgets = server_module._qt()
    nuke = server_module.nuke
    TAG = server_module._TAG

    if current is not None:
        try:
            current.close()
            current.deleteLater()
        except RuntimeError:
            pass  # underlying C++ object already gone

    win = QtWidgets.QWidget()
    win.setWindowTitle("MCP Socket Nuke %s" % server_module.VERSION)
    win.resize(400, 520)

    lay = QtWidgets.QVBoxLayout(win)

    header = QtWidgets.QLabel("<b>MCP Socket Nuke %s</b>" % server_module.VERSION)
    lay.addWidget(header)
    status = QtWidgets.QLabel()
    status.setWordWrap(True)
    lay.addWidget(status)

    def refresh():
        server = server_module._server
        port = server.port if server and server.running else None
        state = "running" if port else "STOPPED"
        scene = server_module._script_name() or "(untitled)"
        variant = server_module._nuke_variant()
        status.setText("port %s | pid %d | %s<br>%s | %s"
                       % (port or "-", os.getpid(), state,
                          nuke.NUKE_VERSION_STRING, os.path.basename(scene)))

    def run_handler(fn):
        """Run a bridge handler in-process and surface the result in the log."""
        try:
            response = server_module._execute_command({"type": fn[0],
                                                       "params": fn[1] or {}})
        except Exception as exc:  # noqa: BLE001
            response = {"status": "error", "message": repr(exc)}
        if response.get("status") == "success":
            res = response.get("result")
            text = res if isinstance(res, str) else json.dumps(
                res, ensure_ascii=False)
            server_module._log_append("stdout", "%s %s" % (fn[0], text or "ok"))
        else:
            server_module._log_append("stderr", "%s FAILED: %s"
                                      % (fn[0], response.get("message", "")))
        refresh_console()
        refresh()

    def toggle_server():
        server = server_module._server
        if server and server.running:
            server_module.stop()
        else:
            server_module.start()
        refresh()

    row = QtWidgets.QHBoxLayout()
    start_btn = QtWidgets.QPushButton("Start")
    start_btn.clicked.connect(lambda: (server_module.start(), refresh()))
    stop_btn = QtWidgets.QPushButton("Stop")
    stop_btn.clicked.connect(lambda: (server_module.stop(), refresh()))
    row.addWidget(start_btn)
    row.addWidget(stop_btn)
    lay.addLayout(row)

    undo_btn = QtWidgets.QPushButton("Undo Agent Work")
    undo_btn.clicked.connect(lambda: run_handler(("undo_agent_session", {})))
    lay.addWidget(undo_btn)

    box = QtWidgets.QGroupBox("Agent Sessions")
    v = QtWidgets.QVBoxLayout(box)
    srow = QtWidgets.QHBoxLayout()
    replay_btn = QtWidgets.QPushButton("Replay Last Session")
    replay_btn.clicked.connect(lambda: run_handler(("replay_last_session", {})))
    logpath_btn = QtWidgets.QPushButton("Copy Log Path")
    logpath_btn.clicked.connect(lambda: _copy_session_path(server_module, QtGui))
    srow.addWidget(replay_btn)
    srow.addWidget(logpath_btn)
    v.addLayout(srow)
    lay.addWidget(box)

    def refresh_console():
        with server_module._LOG_LOCK:
            entries = list(server_module._LOG_RING)[-12:]
        console.setPlainText("\n".join(
            "%s [%s] %s" % (e["ts"], e["stream"], e["text"].replace("\n", " | ")[:220])
            for e in entries))
        bar = console.verticalScrollBar()
        bar.setValue(bar.maximum())

    box = QtWidgets.QGroupBox("Console Log")
    v = QtWidgets.QVBoxLayout(box)
    console = QtWidgets.QPlainTextEdit()
    console.setReadOnly(True)
    console.setMaximumBlockCount(200)
    f = QtGui.QFont("Consolas")
    f.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    console.setFont(f)
    v.addWidget(console)
    brow = QtWidgets.QHBoxLayout()
    save_btn = QtWidgets.QPushButton("Save to File")
    save_btn.clicked.connect(lambda: _save_console(server_module, QtGui))
    clear_btn = QtWidgets.QPushButton("Clear")
    clear_btn.clicked.connect(lambda: run_handler(("clear_console_log", {})))
    refresh_btn = QtWidgets.QPushButton("Refresh")
    refresh_btn.clicked.connect(refresh_console)
    for b in (save_btn, clear_btn, refresh_btn):
        brow.addWidget(b)
    v.addLayout(brow)
    lay.addWidget(box)

    lay.addStretch(1)
    refresh()
    refresh_console()
    win.show()
    print("%s window opened" % TAG)
    return win


def _copy_session_path(server_module, QtGui) -> None:
    path = server_module._last_session_file()
    if path:
        QtGui.QGuiApplication.clipboard().setText(path)
        server_module._log_append("stdout", "session log path copied: %s" % path)
    else:
        server_module._log_append("stderr", "no session log file yet")


def _save_console(server_module, QtGui) -> None:
    with server_module._LOG_LOCK:
        entries = list(server_module._LOG_RING)
    base = os.path.join(tempfile.gettempdir(), "mcp_socket_nuke")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "console_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write("%s [%s] %s\n" % (e["ts"], e["stream"], e["text"]))
    QtGui.QGuiApplication.clipboard().setText(path)
    server_module._log_append("stdout", "console saved: %s (path in clipboard)" % path)
