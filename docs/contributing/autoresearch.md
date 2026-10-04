# Autoresearch loops: speed and security

pydeno's goal is to run JavaScript **as fast and as securely as possible**: clearly ahead of simple Deno
wrappers, on par with Monty. This page is the pre-agreed setup for the
[`autoresearch`](https://github.com/github/awesome-copilot/tree/main/skills/autoresearch) skill
(MIT, from `github/awesome-copilot`; copied unmodified to `.claude/skills/autoresearch/SKILL.md`), so an
agent can run the loop without asking the interactive setup questions.

The skill's loop is: baseline, edit, commit, measure, **keep if better, otherwise `git reset --hard`**, log
to `results.tsv`. Everything below adapts it to this repository.

## Ground rules (they override the skill where they differ)

1. **Work on a dedicated branch or worktree** named `autoresearch/<tag>`; `git reset --hard` is only ever run
   there, never on a shared branch and never in someone else's worktree.
2. **Never push to `main`.** Kept experiments go into a pull request (squash the kept commits into logical
   ones); the PR body contains the `results.tsv` table.
3. **Security beats speed.** A speed change that weakens any limit, check or isolation property is discarded
   even if it wins. Every kept change that touches the worker, the wire, the bridge, the sandbox or the
   journal gets an **independent security review** before it merges (the reviews on PRs #51 to #57 found real
   bugs every time).
4. **A metric you can game is not a metric.** Deleting a global from JavaScript lowers a "globals" count and
   removes no engine code: do not use attack-surface counts that can be satisfied cosmetically. Security is
   measured by hostile probes that must all fail, plus the test suites.
5. **Linux sandbox changes** (`_sandbox.py` seccomp/Landlock, the worker launch) cannot be judged on a Mac.
   They are verified on native x86_64 and aarch64 via a throwaway workflow before they count as kept.
6. **Noise.** The machine may be loaded. The metric scripts print the minimum of several independent medians;
   when a difference is small (under about 5%), re-measure the baseline in the same session before deciding.
7. No new dependencies, no change to the public API without an issue, no downstream product names anywhere.

## Loop S1: warm-call latency

| | |
|---|---|
| Goal | Lower the cost of one `IsolatedRuntime.eval("1 + 1")` after the worker is warm |
| Metric command | `python scripts/autoresearch/metric_speed.py warm` |
| Extraction | the single `METRIC: <microseconds>` line on stdout |
| Direction | lower is better |
| In scope | `python/pydeno/_isolated.py` (`_request`, `_pump`, supervision), `_wire.py`, `_worker.py` (reader/handoff), `_sandbox.py` (readings), `src/runtime/runner/` (dispatch), `src/runtime/python/` |
| Out of scope | the OS sandbox policy (seccomp tables, Seatbelt profile), `ops.rs` bridge security code, tests (except adding tests), docs |
| Constraints | all of `tests/test_isolated_*.py`, `test_sandbox_*.py`, `test_aio_isolated_runtime.py`, `test_sandbox_pool.py` pass; `python scripts/autoresearch/metric_security.py` stays at its baseline or lower; no limit weakened |

## Loop S2: cold start and pool

| | |
|---|---|
| Goal | Lower the time to a ready sandbox |
| Metric commands | `python scripts/autoresearch/metric_speed.py cold` (milliseconds, a new sandbox + first call) and `... checkout` (a ready pooled worker) |
| Direction | lower is better; run `cold` first, then confirm `checkout` did not regress |
| In scope | worker start-up (`_worker.py` imports and `init`), `_sandbox.py` start-up helpers, `_sandbox_pool.py`, `src/runtime/runner/core.rs` (runtime creation), build settings that do not add dependencies |
| Out of scope | anything that must happen after the OS sandbox is applied is not moved before it; no weakening of `sandbox="require"` or the self-test |
| Constraints | as S1, plus: every import the worker needs still happens before the OS sandbox goes up |

## Loop S3: front-door feeds

| | |
|---|---|
| Goal | Lower checkout + 10 small commands |
| Metric command | `python scripts/autoresearch/metric_speed.py feeds10` (and the front-door benchmark once `Pydeno` is on `main`) |
| Direction | lower is better |
| Scope / constraints | as S1; the persistent worker event loop (issue #60) is the first idea |

## Loop X1: hostile-guest probes

| | |
|---|---|
| Goal | No hostile-guest probe succeeds |
| Metric command | `python scripts/autoresearch/metric_security.py` |
| Extraction | `METRIC: <violations>` on stdout; the failing probes are listed on stderr |
| Direction | lower is better; the target is 0 |
| In scope | the bridge (`src/runtime/ops.rs`, `convert.rs`, `core.rs`), `_tools.py`, `_worker.py`, `_isolated.py`, `_sandbox.py` (Linux changes need rule 5) |
| Out of scope | the metric script itself during a loop (see below), tests that currently pass |
| Constraints | the full test suite passes; no probe is removed or weakened; speed metrics do not regress by more than the noise |

**Growing the battery (red-team step, not part of a loop run).** When review or red-teaming finds a new class
of attack, add a probe to `scripts/autoresearch/metric_security.py` first (it should fail), then run loop X1
to fix it. A probe is an attack that returns `True` if the sandbox failed. An unexpected exception inside a
probe counts as a violation. Never delete a probe to make the number smaller.

## Recording

`results.tsv` and `run.log` stay untracked (the skill adds them to `.git/info/exclude`). When a loop ends,
paste the table into the PR and recommend next steps; a loop that plateaus for 5 experiments should try a
different idea class (see the skill's strategy order) or stop and report.
