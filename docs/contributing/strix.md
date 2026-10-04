# Strix: AI pentest review

[Strix](https://github.com/usestrix/strix) is an open-source autonomous pentest agent. We use it as one more
reviewer of the source, next to the probe battery, the independent model reviews and the native Linux runs.
It reads the code and runs small local harnesses; it does not replace OS-level or engine-level testing (the
syscall sweeps and the native x86_64 and aarch64 runs), which it is not built for.

## Run it locally (preferred)

Needs Docker (or a compatible engine) and an LLM key. Findings stay on your machine.

```bash
# a clean export, without vendored bundles and docs
mkdir /tmp/pydeno-export && git archive HEAD | tar -x -C /tmp/pydeno-export
rm -rf /tmp/pydeno-export/vendor /tmp/pydeno-export/docs

export STRIX_LLM="openrouter/<model>"      # or any provider Strix supports
export LLM_API_KEY="..."
strix -n -m standard --max-budget 10 \
  --mount /tmp/pydeno-export \
  --instruction-file .github/strix-instructions.md
```

Reports land in `strix_runs/<run>/` (`penetration_test_report.md`, `vulnerabilities/vuln-*.md`, SARIF and JSON).
A standard run over this repository cost well under one US dollar. Keep `--max-budget` set anyway.

## The GitHub Actions job

`.github/workflows/strix.yml` runs weekly and on demand (`workflow_dispatch`, with a mode and a budget).
It is advisory and is not part of the release gate.

- It needs the repository secrets `STRIX_LLM` and `LLM_API_KEY`; without them the job is skipped.
- The repository and its logs are public, so **findings are not printed or uploaded**: the scan output is
  discarded and only severity counts go to the job summary (`scripts/security/strix_summary.py`). A high or
  critical finding fails the job; medium and below are reported as counts.
- Strix is pinned (`STRIX_VERSION`). Bump it deliberately and compare a run before and after.
- It is not triggered by pull requests: forks have no secrets, and every run costs money.

## What to do with a finding

Treat it like any other review finding: reproduce it, write the failing test first, make the smallest fix,
and have a different model or person review the fix. A severe finding goes to the maintainer privately
before anything is written in a public issue or PR. Add a probe to `scripts/autoresearch/metric_security.py`
when the case can be checked cheaply.
