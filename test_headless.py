# -*- coding: utf-8 -*-
r"""Headless smoke for the Nuke bridge — engine mode (no GUI), in-memory.

Run with the BUNDLED interpreter (Nuke closed or open — engine takes no
interactive seat):

    "C:\Program Files\Nuke17.1v2\python.exe" test_headless.py

Starts the bridge on a test port inside the Nuke engine (GUI=False → direct
dispatch), then talks to it over raw TCP exactly like the stdio server does.
Creates and destroys a few throwaway nodes in memory; nothing is saved.

Expected output ends with: HEADLESS SMOKE PASS (n/n)
"""

import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TEST_PORT = 9991

import nuke  # noqa: E402  (bundled interpreter: starts the Nuke engine)
from mcp_socket_nuke import server  # noqa: E402


def call(port, ctype, params=None, timeout=30):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(json.dumps({"type": ctype, "params": params or {}}).encode("utf-8"))
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                raise RuntimeError("bridge closed before replying")
            buf += chunk
            try:
                return json.loads(buf.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    finally:
        s.close()


def main() -> int:
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        passed += bool(ok)
        failed += not ok
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           (" | " + str(detail)[:160]) if detail else ""))

    if not server.start(TEST_PORT):
        print("FATAL: bridge failed to start on port %d" % TEST_PORT)
        return 1

    # ping
    reply = call(TEST_PORT, "ping")
    check("ping", reply.get("status") == "success",
          reply.get("message", ""))
    if reply.get("status") == "success":
        res = reply["result"]
        check("ping.port == test port", res.get("port") == TEST_PORT, res.get("port"))
        check("ping.version", res.get("nuke_version") == nuke.NUKE_VERSION_STRING,
              res.get("nuke_version"))
        check("ping.variant", bool(res.get("variant")), res.get("variant"))
        check("ping.gui_false", res.get("gui") is False, res.get("gui"))

    # get_scene_info
    reply = call(TEST_PORT, "get_scene_info")
    check("get_scene_info", reply.get("status") == "success",
          reply.get("message", ""))
    if reply.get("status") == "success":
        res = reply["result"]
        check("scene_info.frames", res.get("frame_range") == [1, 100],
              res.get("frame_range"))
        check("scene_info.fps", res.get("fps") == 24.0, res.get("fps"))
        check("scene_info.format", isinstance(res.get("format"), dict)
              and res["format"].get("width") == 640, res.get("format"))

    # get_hierarchy on a known in-memory graph: Constant -> Blur
    const = nuke.createNode("Constant")
    blur = nuke.createNode("Blur")
    blur.setInput(0, const)
    reply = call(TEST_PORT, "get_hierarchy", {"max_nodes": 50})
    check("get_hierarchy", reply.get("status") == "success",
          reply.get("message", ""))
    if reply.get("status") == "success":
        res = reply["result"]
        by_name = {n["name"]: n for n in res.get("nodes", [])}
        check("hierarchy.total == 2", res.get("total_nodes") == 2,
              res.get("total_nodes"))
        blur_entry = by_name.get(blur.name(), {})
        inputs = blur_entry.get("inputs", [])
        check("hierarchy.blur_input",
              inputs and inputs[0]["node"] == const.name()
              and inputs[0]["index"] == 0, inputs)
        check("hierarchy.positions", blur_entry.get("xpos") is not None,
              (blur_entry.get("xpos"), blur_entry.get("ypos")))

    # execute_nuke_code: stdout channel + result var + error channel
    reply = call(TEST_PORT, "execute_nuke_code",
                 {"code": "print('SMOKE_TAG'); result = 21 * 2"})
    check("execute.stdout", reply.get("status") == "success"
          and "SMOKE_TAG" in reply["result"]["stdout"],
          reply.get("message", ""))
    check("execute.result", reply["result"]["returned"] == 42,
          reply["result"]["returned"])
    reply = call(TEST_PORT, "execute_nuke_code", {"code": "1 / 0"})
    check("execute.error_channel", reply.get("status") == "success"
          and reply["result"]["error"] is True
          and "ZeroDivisionError" in reply["result"]["stderr"],
          str(reply["result"].get("stderr", ""))[:80])

    # undo_agent_session in engine mode: honest refusal (no undo stack)
    reply = call(TEST_PORT, "undo_agent_session")
    check("undo.engine_refusal", reply.get("status") == "success"
          and reply["result"].get("undone") == 0,
          str(reply["result"])[:120])

    # registry + session log path
    time.sleep(0.2)
    reply = call(TEST_PORT, "list_instances")
    check("list_instances", reply.get("status") == "success"
          and any(i.get("port") == TEST_PORT
                  for i in reply["result"].get("instances", [])),
          str(reply["result"])[:120])
    reply = call(TEST_PORT, "get_session_log_path")
    check("get_session_log_path", reply.get("status") == "success",
          reply.get("message", ""))

    # unknown command → honest error with the known list
    reply = call(TEST_PORT, "no_such_command")
    check("unknown_command", reply.get("status") == "error"
          and "Known:" in reply.get("message", ""),
          reply.get("message", "")[:100])

    # cleanup the throwaway graph (bridge stop also restores streams)
    server.stop()
    for n in (blur, const):
        try:
            nuke.delete(n)
        except Exception:  # noqa: BLE001
            pass

    print("\nHEADLESS SMOKE %s (%d/%d)"
          % ("PASS" if failed == 0 else "FAIL", passed, passed + failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
