# -*- coding: utf-8 -*-
r"""Install MCP Socket for Nuke into the user's .nuke directory (mcp-socket family).

Run with the SYSTEM Python (no nuke needed), Nuke closed:

    python.exe install_nuke_mcp.py

What it does:
  1. copies the `mcp_socket_nuke/` package (this directory) to ~/.nuke/
  2. copies nuke_mcp.py (stdio MCP server) next to it — the path the ZCode
     config entry (mcp.servers.nuke) points at
  3. ensures ~/.nuke/init.py puts ~/.nuke on sys.path (marker-merged)
  4. ensures ~/.nuke/menu.py starts the bridge and adds the "MCP Socket"
     menu (marker-merged — safe to re-run; never touches foreign content)

Uninstall: delete ~/.nuke/mcp_socket_nuke/, ~/.nuke/nuke_mcp.py and the two
marked blocks in init.py / menu.py.
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_NUKE = os.path.join(os.path.expanduser("~"), ".nuke")

INIT_MARK_OPEN = "# >>> mcp_socket_nuke >>>"
INIT_MARK_CLOSE = "# <<< mcp_socket_nuke <<<"

INIT_BLOCK = INIT_MARK_OPEN + """
import os as _mcp_os
import sys as _mcp_sys
_mcp_here = _mcp_os.path.dirname(_mcp_os.path.abspath(__file__))
if _mcp_here not in _mcp_sys.path:
    _mcp_sys.path.insert(0, _mcp_here)
""" + INIT_MARK_CLOSE

MENU_BLOCK = INIT_MARK_OPEN + """
try:
    import nuke as _mcp_nuke
    from mcp_socket_nuke import server as _mcp_socket_nuke_server
    _mcp_socket_nuke_server.start()
    _mcp_menu = _mcp_nuke.toolbar("Nuke").addMenu("MCP Socket")
    _mcp_menu.addCommand("Open Window",
                         lambda: _mcp_socket_nuke_server.show_window())
except Exception as _mcp_exc:
    print("[MCP_Socket_Nuke] autostart failed: %r" % (_mcp_exc,))
""" + INIT_MARK_CLOSE


def _merge_marker_block(path: str, block: str) -> str:
    """Replace our marked block in `path` (or append it); other content
    (the user's own menu items) is never touched."""
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out, skipping = [], False
    replaced = False
    for line in lines:
        if line.strip() == INIT_MARK_OPEN:
            skipping = True
            if not replaced:
                out.append(block)
                replaced = True
            continue
        if line.strip() == INIT_MARK_CLOSE:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(block)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return ("updated" if os.path.exists(path) else "created")


def main() -> int:
    nuke_running = False
    try:
        import subprocess
        probe = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Nuke17.1.exe"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        nuke_running = "Nuke17.1" in (probe.stdout or "")
    except Exception:  # noqa: BLE001 — best-effort check only
        pass
    if nuke_running:
        print("WARNING: Nuke is running. Close it before installing "
              "(modules are cached in the live session).")

    if not os.path.isdir(HOME_NUKE):
        os.makedirs(HOME_NUKE)

    # 1. package
    src_pkg = os.path.join(HERE, "mcp_socket_nuke")
    dst_pkg = os.path.join(HOME_NUKE, "mcp_socket_nuke")
    if not os.path.isdir(src_pkg):
        print("ERROR: %s not found (run from the source checkout)" % src_pkg)
        return 1
    if os.path.isdir(dst_pkg):
        shutil.rmtree(dst_pkg)
    shutil.copytree(src_pkg, dst_pkg,
                    ignore=shutil.ignore_patterns("__pycache__"))
    print("package  -> %s" % dst_pkg)

    # 2. stdio server
    dst_stdio = os.path.join(HOME_NUKE, "nuke_mcp.py")
    shutil.copy2(os.path.join(HERE, "nuke_mcp.py"), dst_stdio)
    print("stdio    -> %s" % dst_stdio)

    # 3. init.py (sys.path insurance)
    init_path = os.path.join(HOME_NUKE, "init.py")
    state = _merge_marker_block(init_path, INIT_BLOCK)
    print("init.py  -> %s (%s)" % (init_path, state))

    # 4. menu.py (autostart + menu item)
    menu_path = os.path.join(HOME_NUKE, "menu.py")
    state = _merge_marker_block(menu_path, MENU_BLOCK)
    print("menu.py  -> %s (%s)" % (menu_path, state))

    print("\nDone. Start Nuke — the bridge listens on 127.0.0.1:9897 "
          "(auto-offsets +1..+10 when busy).")
    print("ZCode config (mcp.servers.nuke):")
    print('  {"type": "stdio", "command": "<system python>", '
          '"args": ["%s"]}' % dst_stdio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
