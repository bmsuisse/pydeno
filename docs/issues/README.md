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
not an exact-head release artifact.

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
