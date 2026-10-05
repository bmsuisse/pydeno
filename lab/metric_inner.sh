#!/bin/sh
# Inside a container: METRIC (security battery) on the new code, then paired start-up timing of
# the base sandbox (future/0.9) against the new one, same wheel, alternating rounds.
set -u
PY=$(command -v python3)
$PY -m venv /tmp/new && /tmp/new/bin/pip install -q --no-index --find-links /wheels pydeno >/dev/null
$PY -m venv /tmp/old && /tmp/old/bin/pip install -q --no-index --find-links /wheels pydeno >/dev/null
OLD_SITE=$(/tmp/old/bin/python -c "import pydeno,os;print(os.path.dirname(pydeno.__file__))")
cp /src/lab/base/*.py "$OLD_SITE"/
echo "== $(uname -m) $($PY -V) kernel $(uname -r)"
cd /tmp
/tmp/new/bin/python -c "import pydeno, pydeno._sandbox as s; print('new:', pydeno.__file__, len(s._ALLOWED), 'allowed')"
/tmp/old/bin/python -c "import pydeno, pydeno._sandbox as s; print('old:', pydeno.__file__, hasattr(s, '_ALLOWED'))"
/tmp/new/bin/python /src/scripts/autoresearch/metric_security.py 2>/tmp/metric.err | tail -3
tail -5 /tmp/metric.err
for round in 1 2 3 4 5 6; do
  /tmp/old/bin/python /src/lab/timing.py 15 > /tmp/old.$round.json
  /tmp/new/bin/python /src/lab/timing.py 15 > /tmp/new.$round.json
done
$PY - <<'EOF'
import json, statistics as st
def load(kind):
    out = {"cold": [], "cold_first_eval": [], "spare": []}
    rounds = []
    for r in range(1, 7):
        d = json.load(open(f"/tmp/{kind}.{r}.json"))
        rounds.append(d)
        for k in out:
            out[k] += d[k]
    return out, rounds
old, ro = load("old"); new, rn = load("new")
for k in old:
    mo, mn = st.median(old[k]) * 1000, st.median(new[k]) * 1000
    paired = [st.median(b[k]) - st.median(a[k]) for a, b in zip(ro, rn)]
    print(f"TIMING {k:16} old median {mo:7.2f} ms  new median {mn:7.2f} ms  "
          f"diff {mn - mo:+6.2f} ms  per-round paired diffs (ms): "
          + " ".join(f"{p * 1000:+.1f}" for p in paired))
EOF
