"""bun run check: every file compiles, and the server, as main.py makes it, serves MCP in-process (no loops run):
its tools, events and widgets listed, names unique, each widget fed by a read-only tool, the manifest whole."""
import compileall
import importlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("SB_HOME", os.path.join(HERE, "home"))

if not compileall.compile_dir(HERE, quiet=1, rx=re.compile(r"node_modules|\.git|home")):
    sys.exit("check: a file doesn't compile")

pkg = json.load(open(os.path.join(HERE, "package.json")))
manifest = pkg["extension"]
assert manifest.get("name") and manifest.get("command") and manifest.get("shim") == "switchboard", "check: extension manifest"

import sbmcp  # noqa: E402

main = importlib.import_module("main")
app = next((v for v in vars(main).values() if isinstance(v, sbmcp.Server)), None)
assert app, "check: main.py makes no sbmcp.Server"
with sbmcp.Client(app) as mcp:
    tools = mcp.tools()
    events = mcp.events()
    widgets = mcp.request("resources/list").get("resources", []) if app._widgets else []
names = list(tools)
assert len(names) == len(set(names)) and names, "check: tools"
for w in widgets:
    fed = w["_meta"][sbmcp.WIDGET].get("tool")
    assert not fed or tools[fed]["annotations"]["readOnlyHint"], f"check: widget {w['name']} needs a read-only tool"
declared = set(manifest.get("widgets") or [])
assert declared == {w["_meta"][sbmcp.WIDGET]["name"] for w in widgets}, "check: manifest widgets"
speaks = [n for n, t in tools.items() if t["annotations"].get("speaksForPerson")]
print(f"{manifest['name']}: {len(names)} tools ({len(speaks)} speak for the user), {len(events)} events, {len(widgets)} widgets")
