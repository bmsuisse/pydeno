# OpenCode follow-up issue bodies and validation

The implementation is on `feat/opencode-takeaways`, based on PR #105 head
`bee3195ec73817dc62d38f462327d2b66ba5c206`. These issue bodies are prepared but were not
posted: the GitHub CLI could not reach `api.github.com`. The branch push did succeed.

| Issue body | Implementation |
| --- | --- |
| [Fair prompt catalog](opencode-tool-catalog.md) | `describe_tool_catalog()` with an opt-in character budget and namespace fairness; preserves complete, entry-local schema descriptions. |
| [Async lifecycle coverage](opencode-async-lifecycle.md) | Six bounded tests and a verified closed-session dispatch fix; no promise-semantic changes. |
| [Output boundaries](opencode-output-boundaries.md) | Bounded five-entry dictionary preview snapshot; result/log/HTTP and wire-boundary tests. |

## Verified locally

The combined pure Python suite passed **23 tests**, with the native wire test explicitly
excluded. It covers the new catalog, schema-name collisions, existing description goldens,
UTF-8/JSON/base64 result accounting, console truncation, HTTP capped reads and dictionary
preview traversal. Import-only test setup reported the unavailable native scanner unsupported;
no source scanning or OS confinement was tested by that suite.

The preview regression failed before the fix: twenty dictionary entries were visited to show
five. The fix visits five and preserves the preview text. No wall-clock speed claim is made.
For a 100-tool / four-namespace fixture, complete prompts used 73,300 characters; the bounded
prompt used 8,460, including 7,569 entry characters under its 8,000-character entry budget.
Instructions and namespace summaries are additional to the entry budget.

Ruff, formatting and diff checks passed. Independent review caught an ambiguous schema-name
case; separate entry scopes and a regression addressed it. Each change received independent
review. The first Linux invocations were denied before collection. A later direct Podman run
reproduced a late-dispatch cleanup defect; the closed-session guard then passed all
172 selected async tests on native Linux ARM64 CPython 3.12 (zero failures or skips),
including six new lifecycle cases. The initial combined ARM64 run also passed the
native wire-cap case; its one lifecycle failure is addressed by the guard. These runs
used the retained 0771370 compiled release wheel with the current Python overlay,
not an exact-head release artifact. A CPython 3.14 async run passed 41 tests with zero
skips. With the integration subpackage correctly overlaid and pydantic-ai installed,
89 catalog/lifecycle/output/result tests passed on native ARM64 CPython 3.12, including
the preview and wire regressions. The overlay runner now copies Python subpackages
recursively without replacing the native extension.

## Required before merge

Run against a freshly built compatible native artifact, on Linux ARM64 and x86_64. The cached
macOS extension lacks the current source scanner, and the retained Linux wheel predates this
Python branch; neither is exact-head release proof.

```sh
CONTAINER_RUNTIME=podman OVERLAY_PY=1 \
PYTEST_TARGETS='tests/test_tool_catalog_budget.py tests/test_agent_async_lifecycle.py tests/test_agent_output_boundaries.py tests/test_agent_sandbox.py tests/test_agent_schema_tools.py tests/test_aio_agent.py tests/test_aio_agent_recovery.py tests/test_aio_agent_results.py tests/test_agent_execution_result.py' \
OUT_DIR=/private/tmp/opencode-native KEEP_OUT=1 \
scripts/linux_matrix.sh WHEEL_DIRECTORY python:3.12-slim default
```

For release evidence use exact-head wheels and `OVERLAY_PY=0`. Install the pydantic-ai test
extra in the applicable integration job so the preview regression is collected. Preserve
existing platform deselection rules and report collection counts; do not weaken skip budgets.

## Publish the issues when GitHub API access works

First check for existing issues with these titles to avoid duplicates, then:

```sh
gh issue create --repo bmsuisse/pydeno --title 'Add fair bounded tool catalogs across namespaces' --body-file docs/issues/opencode-tool-catalog.md
gh issue create --repo bmsuisse/pydeno --title 'Extend async agent lifecycle regression coverage' --body-file docs/issues/opencode-async-lifecycle.md
gh issue create --repo bmsuisse/pydeno --title 'Bound model preview work and pin independent output budgets' --body-file docs/issues/opencode-output-boundaries.md
```

Link the created issues from the follow-up PR. PR #105 and these changes must retain their
exact-head CI gates; no merge, release tag or security guarantee is implied by the local runs.
# Issue notes and research takeaways

Research notes and prepared issue bodies. They propose work; they do not change code.

| Note | Subject |
| --- | --- |
| [Omnigent takeaways](omnigent-takeaways.md) | Review of omnigent-ai/omnigent against the agent surface: an `http_fetch` address gap, a tool-call hook design, loop detection, and what not to copy. |

The OpenCode follow-up notes (`opencode-*.md`) and their own index live on `feat/opencode-takeaways`;
they were not on `future/0.10` when this index was created. Merge the two indexes when that branch lands.
# Issue drafts and research notes

| Document | What it is |
| --- | --- |
| [Goose Code Mode takeaways](goose-codemode-takeaways.md) | Research of goose's code-execution mode (and its pctx engine) against pydeno's agent surface: what pydeno already does, seven proposed changes with priority and risk, and what is not worth copying. No code changed. |

The earlier OpenCode follow-ups (`opencode-*.md`) live on PR #114 (`feat/opencode-takeaways`) and
are indexed in that branch's version of this file; merge the two tables when both land.
# Research notes and follow-up issues

Evidence-linked notes comparing pydeno with other projects, each ending in proposed changes with a
priority and a risk. They are proposals, not commitments.

| Note | Subject |
| --- | --- |
| [TrueForge takeaways](trueforge-takeaways.md) | Agent harness (TypeScript, process sandbox): result source, tool annotations, oversized-result preview, per-call tool deadline, backend contract suites. No V8, WASM or Deno content. |
