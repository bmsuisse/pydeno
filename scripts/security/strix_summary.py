"""Count Strix findings by severity without ever printing their text.

The repository is public and so are its Actions logs, so the findings themselves must not reach
them: they are read locally (`strix_runs/<run>/`) by the maintainer. This prints only the severity
counts, writes them to the job summary, and exits 1 when a high or critical finding exists.
Medium and below are reported and left to the maintainer.
"""

from __future__ import annotations

import collections
import json
import os
import pathlib
import sys


def main(root: str = "strix_runs") -> int:
    counts: collections.Counter[str] = collections.Counter()
    runs = sorted(pathlib.Path(root).glob("*/vulnerabilities.json"))
    if not runs:
        print(
            "strix: no vulnerabilities.json found (the scan did not finish or found nothing)"
        )
        return 0
    for report in runs:
        for item in json.loads(report.read_text()):
            counts[str(item.get("severity", "unknown")).lower()] += 1
    line = ", ".join(f"{k}: {counts[k]}" for k in sorted(counts)) or "none"
    print(f"strix findings by severity: {line}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(
                f"### Strix\n\nFindings by severity: **{line}**\n\n"
                "Details are kept off public logs on purpose. Run the scan locally "
                "(`docs/contributing/strix.md`) to read them.\n"
            )
    return 1 if counts["high"] or counts["critical"] else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
