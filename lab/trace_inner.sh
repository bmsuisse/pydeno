#!/bin/sh
# Inside a container: trace the workers of the builtin workload and (optionally) test suites.
# Mounts: /src (repo, ro), /wheels (ro), /out (rw). Env: OVERLAY_PY, TRACE_TAG, TRACE_TESTS.
set -u
. /etc/os-release
export DEBIAN_FRONTEND=noninteractive
case "${ID}:${VERSION_ID:-}" in
  almalinux:*|rocky:*|centos:*|ol:*) PY=python3.12; dnf install -y -q python3.12 python3.12-pip strace >/dev/null 2>&1 ;;
  amzn:*)    PY=python3.11; dnf install -y -q python3.11 python3.11-pip strace >/dev/null 2>&1 ;;
  fedora:*)  PY=python3; dnf install -y -q python3 python3-pip strace >/dev/null 2>&1 ;;
  debian:*|ubuntu:*)
    PY=python3
    apt-get update -qq >/dev/null 2>&1
    if command -v python3 >/dev/null && python3 -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null && [ -x /usr/local/bin/python3 ]; then
      apt-get install -y -qq strace >/dev/null 2>&1
    else
      apt-get install -y -qq python3 python3-venv python3-pip strace >/dev/null 2>&1
    fi ;;
  *) PY=python3 ;;
esac
command -v strace >/dev/null || { echo "no strace"; exit 3; }
$PY -m venv /tmp/v || exit 3
/tmp/v/bin/pip install -q --no-index --find-links /wheels pydeno >/dev/null || { echo "no wheel for $($PY -V)"; exit 3; }
/tmp/v/bin/pip install -q "pytest>=8.4.0" "pytest-asyncio>=1.2.0" "hypothesis>=6.100.0" >/dev/null
if [ "${OVERLAY_PY:-0}" = "1" ]; then
  SITE=$(/tmp/v/bin/python -c "import pydeno,os;print(os.path.dirname(pydeno.__file__))")
  cp /src/python/pydeno/*.py "$SITE"/
fi
TAG=$(uname -m)-${ID}${VERSION_ID:-}-$($PY -c 'import sys;print("py%d%d"%sys.version_info[:2])')-${TRACE_TAG:-base}
T=/tmp/trace-$TAG
rm -rf "$T"; mkdir -p "$T"
$PY /src/scripts/trace_worker_syscalls.py wrap /tmp/v "$T"
mkdir -p /work && cp -r /src/tests /src/vendor /src/examples /work/ && cp /src/pyproject.toml /src/CLAUDE.md /work/
cd /work
echo "== $TAG glibc $(ldd --version 2>/dev/null | head -1 | grep -o '[0-9.]*$') kernel $(uname -r)"
/tmp/v/bin/python /src/scripts/trace_worker_syscalls.py workload > /out/workload-$TAG.log 2>&1; echo "workload exit $?"
tail -12 /out/workload-$TAG.log
if [ -n "${TRACE_TESTS:-}" ]; then
  timeout "${TRACE_TEST_TIMEOUT:-2400}" /tmp/v/bin/python -m pytest $TRACE_TESTS -q -p no:randomly -p no:cacheprovider --no-header > /out/pytest-$TAG.log 2>&1
  echo "pytest exit $?"; tail -5 /out/pytest-$TAG.log
fi
$PY /src/scripts/trace_worker_syscalls.py report "$T" /out/syscalls-$TAG.json > /out/report-$TAG.txt
cat /out/report-$TAG.txt
rm -rf "$T"
