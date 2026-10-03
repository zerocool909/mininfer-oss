#!/usr/bin/env bash
# Validate the *shipped* knowledge graph, not the pre-build extraction.
#
# Why this exists: graphify's Step 4.5 health gate runs on the merged extraction,
# which is an intermediate. Its numbers (e.g. "172 dangling-endpoint edges") do
# not describe graphify-out/graph.json, because build_from_json mints external
# stub nodes for import targets (#2873) and collapses parallel relations to one
# edge per node pair by design (#1061). Reporting those raw numbers as a defect
# in the graph is a false alarm. This script measures what is actually shipped.
#
# Exit 0 = clean. Exit 1 = dangling/missing endpoints or duplicate node ids.
# Self-loops on `calls` are reported but not fatal: a recursive call is real
# structure.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRAPH="$ROOT/graphify-out/graph.json"
PY_FILE="$ROOT/graphify-out/.graphify_python"

if [ ! -f "$GRAPH" ]; then
    echo "no graph at $GRAPH - run /graphify . first" >&2
    exit 2
fi

# Reuse the interpreter the graph was built with, so the version matches.
if [ -f "$PY_FILE" ]; then
    PY="$(cat "$PY_FILE")"
else
    PY="python3"
fi

"$PY" - "$GRAPH" <<'PYEOF'
import json
import sys
from collections import Counter
from pathlib import Path

graph_path = Path(sys.argv[1])
try:
    data = json.loads(graph_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"graph.json is not valid JSON: {exc}", file=sys.stderr)
    raise SystemExit(1)

nodes = data.get("nodes", [])
links = data.get("links") or data.get("edges") or []
ids = [n.get("id") for n in nodes]
id_set = set(ids)

print(f"graph: {graph_path}")
print(f"  directed={bool(data.get('directed'))} multigraph={bool(data.get('multigraph'))}")
print(f"  nodes={len(ids)} links={len(links)}")

# A duplicate id means two distinct source symbols normalised to one id. The
# later one silently wins; any edge naming that id is ambiguous.
dupe_ids = [i for i, n in Counter(ids).items() if n > 1]

dangling = [e for e in links
            if e.get("source") not in id_set or e.get("target") not in id_set]

self_loops = [e for e in links if e.get("source") == e.get("target")]

# Post-build, a repeated ordered pair means to_json emitted two links for one
# node pair, which no simple-graph loader can represent.
pairs = Counter((e.get("source"), e.get("target")) for e in links)
collapsed = sum(v - 1 for v in pairs.values() if v > 1)

external = sum(1 for n in nodes if n.get("external") or n.get("type") == "external")

print(f"  external stub nodes={external}")
print(f"  dangling endpoints={len(dangling)}")
print(f"  duplicate node ids={len(dupe_ids)}")
print(f"  collapsed parallel links={collapsed}")
print(f"  self-loops={len(self_loops)}")

for nid in dupe_ids:
    print(f"    DUPLICATE ID {nid}")
for e in dangling:
    print(f"    DANGLING {e.get('source')} -> {e.get('target')} ({e.get('relation')})")
for e in self_loops:
    print(f"    self-loop {e.get('source')} ({e.get('relation')}) at "
          f"{e.get('source_file')}:{e.get('source_location')}")

fatal = bool(dangling or dupe_ids)
if fatal:
    print("GRAPH INVALID: dangling endpoints or duplicate ids "
          "(a loader will materialise phantom nodes).")
    raise SystemExit(1)

print("Graph OK.")
raise SystemExit(0)
PYEOF
