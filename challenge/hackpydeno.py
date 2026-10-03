"""Tiny client for the Hack pydeno server (standard library only).

python hackpydeno.py --url http://localhost:8080 --passphrase "my secret phrase" exploit.js
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True, help="server base URL")
    p.add_argument("--passphrase", help="hashed (SHA-256) and sent as the User header")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("file", help="JavaScript file to run ('-' for stdin)")
    a = p.parse_args()
    code = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    headers = {"Content-Type": "application/json"}
    if a.passphrase:
        digest = hashlib.sha256(a.passphrase.encode()).hexdigest()
        headers["User"] = digest
        print(f"user: {digest}", file=sys.stderr)
    req = urllib.request.Request(
        a.url.rstrip("/") + "/run",
        data=json.dumps({"code": code}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except urllib.error.URLError as exc:
        print(f"request failed: {exc.reason}", file=sys.stderr)
        return 2
    try:
        parsed = json.loads(body)
        print(json.dumps(parsed, indent=2))
        return 0 if status == 200 and parsed.get("ok") else 1
    except ValueError:
        print(body.decode("utf-8", "replace"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
