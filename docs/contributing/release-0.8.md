# 0.8 integration branch

`future/0.8` collects the reviewed security and speed work, plus the Monty-shaped front door, into one release
candidate. It is a **draft PR against `main`**: nothing here merges to `main` until the 0.7.0 verification run has
finished and the checklist below is green. The working agreement is in issue #68; the autoresearch loops and their
metrics are in `docs/contributing/autoresearch.md`.

## Goal of 0.8

The fastest and most secure way to run AI-generated JavaScript from Python, with one easy entry point in the
shape of Monty's (`Pydeno().checkout() ... session.feed_run(...)`), secure and fast by default.

## How two sessions work on this branch

- **Small sub-PRs into `future/0.8`** (base branch `future/0.8`), one per issue, failing-first tests. The
  integration owner (maintainer session) merges the PRs listed below into this branch in order.
- If you must push to `future/0.8` directly: `git pull --rebase` first, small commits, **never force-push**, and say
  in the commit message which issue or PR it belongs to.
- **Independent review** for anything touching the worker, wire, bridge, supervision, sandbox policy or journals
  (see #68). Reviewed-clean items are ticked below.
- Speed claims need release-build, paired A/B numbers (`scripts/autoresearch/metric_speed.py`); security claims need
  the probe battery at 0 (`scripts/autoresearch/metric_security.py`).
- Linux sandbox changes need native x86_64 and aarch64 runs before they are ticked.

## Checklist (merge order)

Items already merged to `main` are not listed. An item is ticked when its PR is merged **into this branch**. The
branch as a whole is validated (full suite plus the two metric scripts) before it is marked ready; the result is
recorded in a comment on the PR.

- [x] #59 CI: trim duplicate runs, fix the report gate and cross-platform test blockers
- [x] #61 autoresearch skill, loop setup and metric scripts
- [x] #52 bridge: fail closed on a poisoned bind target; timeouts enforced when guest code customises `Error.prototype` (reviewed, 5 rounds)
- [ ] #64 runtime stays usable after a module evaluation times out (stacked on #52; review running)
- [ ] #63 buffer accounting and console hardening (stacked on #52; re-review pending)
- [x] #53 warm-call overhead (reviewed clean)
- [x] #62 warm-call autoresearch loop (stacked on #53; reviewed clean). **Merge note:** it and #52 both rewrite the
      watchdog loop in `src/runtime/runner/termination.rs`; keep `parked` set from the final next deadline (after
      #52's re-issue adjustment) and re-run the termination stress test
- [x] #55 cold start (reviewed clean)
- [x] #51 `http_fetch` (reviewed clean)
- [x] #57 front door `Pydeno` / `AsyncPydeno` (reviewed clean after 5 rounds)
- [ ] #49 CLI and `llm` plugin (needs independent review)
- [ ] #54 inspector cargo feature (CI pending)
- [ ] #67 frame-queue bound and copy reduction (review running; overlaps #62)
- [ ] #71 supervisor termination authority: refuse guest code if the hardened worker cannot be signalled (Codex owns; native Linux runs and independent review needed; exploit details stay private until fixed)
- [ ] #56 / PR #69 Linux resource-probe visibility after worker privilege hardening (Codex)
- [ ] #45 seccomp allow-list / kill-on-violation (needs native x86_64 and aarch64)
- [ ] #60 persistent worker event loop (open)
- [ ] #42 strict eval profile (open)

## Documentation

- [ ] New README focused on the Monty-shaped entry, AI code mode, the sandbox and the security model (this PR). Rules: every number is measured (release builds), every claim is true of this branch, no unmerged feature is promised.
- [ ] Alternatives page updated with release-build numbers once the speed PRs are validated together.

## Release gates for 0.8.0

1. 0.7.0 tagged and published first.
2. All boxes above ticked or explicitly deferred with a reason.
3. Full local suite on macOS, plus the Linux container matrix and the Platforms matrix on this branch
   (`gh workflow run platforms.yml --ref future/0.8`).
4. `metric_security.py` reports 0; the alternatives page is updated with release-build numbers.
5. A security-release note (0.7.1 or part of 0.8.0) covers the hardening of already-released behaviour.
