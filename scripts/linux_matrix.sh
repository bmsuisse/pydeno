#!/usr/bin/env bash
# Run the isolation tests inside one Linux image, against a prebuilt wheel, under one
# simulated kernel profile. Works with podman or docker, locally and in CI.
#
#   scripts/linux_matrix.sh WHEEL_DIR IMAGE [PROFILE]
#
#   WHEEL_DIR  directory of manylinux wheels (one per CPython minor); pip picks the match
#   IMAGE      debian:bookworm, ubuntu:22.04, fedora:latest, almalinux:9, python:3.12-slim, ...
#   PROFILE    default | no-landlock | no-seccomp | none      (default: default)
#
# Containers share the host kernel, so this cannot vary the kernel itself. What it can do is
# hide kernel features from the container with a seccomp profile, which exercises the paths
# that matter most: the sandbox degrading gracefully, and `sandbox="require"` failing closed.
#
#   default      the kernel decides; expect landlock+seccomp on any kernel >= 5.13
#   no-landlock  landlock_* syscalls return ENOSYS; expect seccomp
#   no-seccomp   the seccomp() syscall returns EPERM; expect landlock
#   none         both hidden; expect none, and sandbox="require" must refuse to start
#
# Environment:
#   CONTAINER_RUNTIME  podman or docker (default: whichever is on PATH, podman first)
#   PYTEST_TARGETS     what to run (default: the isolation and escape suites)
#   OVERLAY_PY=1       copy ./python/pydeno/*.py over the installed wheel, for iterating on
#                      Python-only changes without rebuilding the wheel (local use only)
#   MIN_TESTS         minimum selected test count (default: 1 for focused local runs)
#   KEEP_OUT=1         leave the junit/collect logs in OUT_DIR
#   EXTRA_RUN_ARGS     extra flags for `run` (CI passes `--security-opt apparmor=unconfined`
#                      so a host AppArmor profile cannot change which sandbox layers apply)
set -euo pipefail

WHEELS=${1:?usage: linux_matrix.sh WHEEL_DIR IMAGE [PROFILE]}
IMAGE=${2:?usage: linux_matrix.sh WHEEL_DIR IMAGE [PROFILE]}
PROFILE=${3:-default}

RUNTIME=${CONTAINER_RUNTIME:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WHEELS=$(cd "$WHEELS" && pwd)
OUT=${OUT_DIR:-$(mktemp -d)}
mkdir -p "$OUT"
# The monty-parity file is left out by default: its strict xfails run the in-process crash
# probes for minutes and do not depend on the kernel. One cell runs it (PYTEST_TARGETS=...).
TARGETS=${PYTEST_TARGETS:-"tests/test_isolated_runtime.py tests/test_isolated_lifecycle.py tests/test_isolated_determinism.py tests/test_isolated_fuzz.py tests/test_snapshot_auth.py tests/test_redteam_syscalls.py tests/test_sandbox_syscall_tables.py tests/test_known_escape_techniques.py tests/test_guest_globals.py tests/test_isolated_libraries.py tests/test_isolated_review_findings.py tests/test_aio_isolated_runtime.py tests/test_isolated_limits.py tests/test_status.py tests/test_sandbox_attest.py tests/test_sandbox_attest_edges.py tests/test_isolated_command_loop.py tests/test_isolated_wasm.py"}

case "$PROFILE" in
  default)     BLOCK=""; EXPECT="landlock+seccomp" ;;
  no-landlock) BLOCK="landlock_create_ruleset landlock_add_rule landlock_restrict_self"; EXPECT="seccomp" ;;
  no-seccomp)  BLOCK="seccomp"; EXPECT="landlock" ;;
  none)        BLOCK="landlock_create_ruleset landlock_add_rule landlock_restrict_self seccomp"; EXPECT="none" ;;
  *) echo "unknown profile: $PROFILE" >&2; exit 2 ;;
esac
# Kernels older than 5.13 have no Landlock; let the caller say so rather than guess.
if [ "${KERNEL_HAS_LANDLOCK:-1}" = "0" ]; then
  case "$EXPECT" in
    landlock+seccomp) EXPECT="seccomp" ;;
    landlock) EXPECT="none" ;;
  esac
fi

# Allow everything except the blocked calls. (A profile that denied by default would also
# take away what the container runtime itself needs.)
{
  printf '{"defaultAction":"SCMP_ACT_ALLOW","syscalls":['
  if [ -n "$BLOCK" ]; then
    names=""
    for n in $BLOCK; do names="$names\"$n\","; done
    printf '{"names":[%s],"action":"SCMP_ACT_ERRNO","errnoRet":%s}' "${names%,}" \
      "$([ "$PROFILE" = no-seccomp ] && echo 1 || echo 38)"
  fi
  printf ']}'
} > "$OUT/seccomp-$PROFILE.json"

INNER=$OUT/inner.sh
cat > "$INNER" <<'INNER_EOF'
#!/bin/sh
set -eu
. /etc/os-release
# Which interpreter to test on each distro. The enterprise families ship a Python older than
# pydeno supports as `python3` (3.9), so they get a newer one installed alongside it.
case "${ID}:${VERSION_ID:-}" in
  almalinux:*|rocky:*|centos:*|ol:*) PY=python3.12 ;;
  amzn:*)                            PY=python3.11 ;;
  opensuse*)                         PY=python3.13 ;;
  *)                                 PY=python3 ;;
esac

bootstrap() {
  case "${ID}:${VERSION_ID:-}" in
    debian:*|ubuntu:*)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq >/dev/null
      apt-get install -y -qq python3 python3-venv python3-pip >/dev/null ;;
    fedora:*)  dnf install -y -q python3 python3-pip >/dev/null ;;
    almalinux:*|rocky:*|centos:*|ol:*) dnf install -y -q python3.12 python3.12-pip >/dev/null ;;
    amzn:*)    dnf install -y -q python3.11 python3.11-pip >/dev/null ;;
    arch:*)    pacman -Sy --noconfirm --quiet python python-pip >/dev/null ;;
    opensuse*) zypper -q install -y python313 python313-pip >/dev/null ;;
    *) echo "no bootstrap recipe for ${ID}:${VERSION_ID:-}" >&2; exit 3 ;;
  esac
}

if ! command -v "$PY" >/dev/null 2>&1; then bootstrap; fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
  || { echo "Python is older than 3.10 here; use a newer image" >&2; exit 3; }

"$PY" -m venv /tmp/v
/tmp/v/bin/pip install -q --no-index --find-links /wheels pydeno >/dev/null
/tmp/v/bin/pip install -q "pytest>=8.4.0" "pytest-asyncio>=1.2.0" "hypothesis>=6.100.0" >/dev/null

if [ "${OVERLAY_PY:-0}" = "1" ]; then
  SITE=$(/tmp/v/bin/python -c "import pydeno,os;print(os.path.dirname(pydeno.__file__))")
  cp /src/python/pydeno/*.py "$SITE"/
fi

/tmp/v/bin/python -c "
from pydeno import IsolatedRuntime
r = IsolatedRuntime()
print('layers:', r.sandbox, '| extras:', r.sandbox_extras or 'none', '| uid:', __import__('os').getuid())
r.close()
" || echo "layers: could not start a worker"

mkdir -p /work
cp -r /src/tests /src/vendor /src/examples /src/docs /src/scripts /work/
cp /src/pyproject.toml /src/CLAUDE.md /work/
cd /work
echo "== $(. /etc/os-release; echo "$PRETTY_NAME") | $(/tmp/v/bin/python -V) | glibc $(ldd --version 2>/dev/null | head -1 | grep -o '[0-9.]*$' || echo '?') | kernel $(uname -r) | $(uname -m)"
# shellcheck disable=SC2086
/tmp/v/bin/python /src/scripts/collect_guard.py 180 $PYTEST_TARGETS --co -q -p no:randomly --strict-markers --strict-config \
  | tee /out/collect.log | tail -1
set +e
# shellcheck disable=SC2086
/tmp/v/bin/python -m pytest $PYTEST_TARGETS -q --no-header -p no:randomly -p no:cacheprovider \
  --strict-markers --strict-config -rsfE --junitxml=/out/junit.xml > /out/pytest.log 2>&1
CODE=$?
tail -${TAIL_LINES:-25} /out/pytest.log
exit "$CODE"
INNER_EOF
chmod +x "$INNER"

echo ">>> $IMAGE  profile=$PROFILE  expect=$EXPECT  runtime=$RUNTIME"
set +e
# shellcheck disable=SC2086
"$RUNTIME" run --rm ${EXTRA_RUN_ARGS:-} \
  --security-opt "seccomp=$OUT/seccomp-$PROFILE.json" \
  -v "$ROOT:/src:ro" -v "$WHEELS:/wheels:ro" -v "$OUT:/out" \
  -e PYDENO_EXPECT_SANDBOX="$EXPECT" -e PYTEST_TARGETS="$TARGETS" -e OVERLAY_PY="${OVERLAY_PY:-0}" -e TAIL_LINES="${TAIL_LINES:-25}" \
  -e PYDENO_REDTEAM_CONTAINER=1 \
  "$IMAGE" sh /out/inner.sh
STATUS=$?
set -e

# Judge the run by its report, not only its exit code: the repo budgets skips at zero and
# xfails at a known number, and a container that quietly ran half the suite must not pass.
if [ -f "$OUT/junit.xml" ]; then
  python3 "$ROOT/scripts/check_test_report.py" --label "$IMAGE/$PROFILE" \
    --junit "$OUT/junit.xml" --collect-log "$OUT/collect.log" \
    --min-tests "${MIN_TESTS:-1}" --max-skipped "${MAX_SKIPPED:-0}" --max-xfailed "${MAX_XFAILED:-10}" \
    || STATUS=1
else
  echo "::error::$IMAGE/$PROFILE: no junit report; the container did not finish" >&2
  STATUS=1
fi

[ "${KEEP_OUT:-0}" = "1" ] || { [ -n "${OUT_DIR:-}" ] || rm -rf "$OUT"; }
if [ "$STATUS" -eq 0 ]; then echo "RESULT PASS  $IMAGE  $PROFILE"; else echo "RESULT FAIL  $IMAGE  $PROFILE"; fi
exit "$STATUS"
