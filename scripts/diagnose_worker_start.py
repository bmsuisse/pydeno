"""Print why an isolated worker does not start on this host (CI diagnosis; safe to run anywhere)."""

from __future__ import annotations

import subprocess
import sys
import traceback


def sh(*cmd: str) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=20, check=False
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"<{exc}>"


print("cpu:", sh("sysctl", "-n", "machdep.cpu.brand_string"))
print("features:", sh("sysctl", "-n", "machdep.cpu.features")[:300])
print("leaf7:", sh("sysctl", "-n", "machdep.cpu.leaf7_features")[:300])
print("python:", sys.version.replace("\n", " "), sys.platform)

import pydeno  # noqa: E402

print("status:", pydeno.sandbox_status())
for mode in ("require", "auto", "off"):
    for flags in ([], ["--jitless"]):
        label = f"sandbox={mode} v8_flags={flags}"
        try:
            kwargs = {"sandbox": mode}
            if flags:
                kwargs["v8_flags"] = flags
            with pydeno.IsolatedRuntime(**kwargs) as rt:
                print(label, "->", rt.eval("1 + 1"))
        except BaseException as exc:  # noqa: BLE001
            print(
                label,
                "-> FAILED",
                type(exc).__name__,
                str(exc)[:1500].replace("\n", " | "),
            )
            traceback.print_exc(limit=2)
