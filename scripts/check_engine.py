#!/usr/bin/env python3
"""Is there a newer V8 we could ship? Exits 1 only when upgrading `deno_core` would raise it.

V8 security fixes reach pydeno only through `deno_core`, which pins the `v8` crate. Newer
`deno_core` releases are not automatically newer in V8 (0.410+ moved to the `deno_v8` facade, which
resolved to an older V8 pre-release), so "latest" is not the question; "does it carry a higher V8
than the one in Cargo.lock?" is.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

API = "https://crates.io/api/v1/crates/"


def get(path: str) -> dict:
    req = urllib.request.Request(
        API + path, headers={"User-Agent": "pydeno-engine-check"}
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def locked(name: str) -> str:
    text = (Path(__file__).resolve().parent.parent / "Cargo.lock").read_text()
    return re.search(rf'name = "{name}"\nversion = "([^"]+)"', text).group(1)


have_core, have_v8 = locked("deno_core"), locked("v8")
latest = get("deno_core")["crate"]["max_stable_version"]
deps = get(f"deno_core/{latest}/dependencies")["dependencies"]
ENGINES = ("v8", "v8x", "deno_v8")
engines = [d for d in deps if d["crate_id"] in ENGINES and d["kind"] == "normal"]
print(f"locked: deno_core {have_core}, v8 {have_v8}")
print(f"latest deno_core {latest} uses {[(d['crate_id'], d['req']) for d in engines]}")

for d in list(
    engines
):  # `deno_v8` is a facade over optional v8/v8x backends: look one level down
    if d["crate_id"] == "deno_v8":
        inner = get(f"deno_v8/{d['req'].lstrip('^')}/dependencies")["dependencies"]
        engines += [
            i for i in inner if i["crate_id"] in ("v8", "v8x") and i["kind"] == "normal"
        ]
        print(
            f"  deno_v8 {d['req']} offers {[(i['crate_id'], i['req']) for i in inner if i['crate_id'] in ('v8', 'v8x')]}"
        )

best = max(
    int(re.search(r"\d+", d["req"]).group())
    for d in engines
    if d["crate_id"] != "deno_v8"
)
if best > int(have_v8.split(".")[0]):
    print(f"UPGRADE: deno_core {latest} can carry V8 crate {best}.x > {have_v8}")
    sys.exit(1)
print("ok: no usable newer V8 via deno_core")
