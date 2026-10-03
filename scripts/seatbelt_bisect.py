"""Diagnostic: which Seatbelt rule makes V8 abort at startup on some macOS versions?

For each deny rule of the worker profile, start a child that applies the profile *without* that rule
and creates a Runtime. Prints which removals make the child survive. Throwaway; not part of the library.
"""

import subprocess
import sys

from pydeno import _sandbox

profile = _sandbox._SEATBELT_PROFILE
lines = profile.splitlines()
# group continuation lines into rules
rules: list[tuple[int, int]] = []
i = 0
while i < len(lines):
    if lines[i].startswith("(deny") or lines[i].startswith("(allow"):
        j = i
        depth = lines[i].count("(") - lines[i].count(")")
        while depth > 0:
            j += 1
            depth += lines[j].count("(") - lines[j].count(")")
        rules.append((i, j))
        i = j + 1
    else:
        i += 1

CHILD = """
import sys, ctypes, ctypes.util
import pydeno
prof = open(sys.argv[1]).read()
lib = ctypes.CDLL(ctypes.util.find_library("sandbox"))
lib.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
err = ctypes.c_char_p()
rc = lib.sandbox_init(prof.encode(), 0, ctypes.byref(err))
if rc != 0:
    print("init failed", err.value); sys.exit(3)
rt = pydeno.Runtime()
print("RESULT", rt.eval("1+1"))
"""


def run(prof: str) -> str:
    with open("/tmp/_prof.sb", "w") as f:
        f.write(prof)
    p = subprocess.run(
        [sys.executable, "-c", CHILD, "/tmp/_prof.sb"], capture_output=True, text=True
    )
    return "ok" if "RESULT 2" in p.stdout else f"rc={p.returncode} {p.stderr.strip()[-120:]}"


print("full profile:", run(profile))
for a, b in rules:
    cut = "\n".join(lines[:a] + lines[b + 1 :])
    r = run(cut)
    if r == "ok":
        print("REMOVING FIXES IT:", " ".join(lines[a : b + 1])[:160])
print("done")
