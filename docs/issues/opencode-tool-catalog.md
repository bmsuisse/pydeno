# Add a fair, bounded prompt catalog for tools across namespaces

Status: local issue draft; GitHub publication pending connectivity.

The existing `describe_tools()` prints every supplied tool. Hosts with multiple bound namespaces need a deterministic way to fit complete descriptions into a prompt budget without one large namespace taking all space.

Add `describe_tool_catalog(namespaces, max_chars=8000)`, a description-only helper. The budget counts complete tool-entry characters; the execution instructions and per-namespace count summaries are outside it. Select cheapest complete entries round-robin across sorted namespaces; label COMPLETE/PARTIAL and shown/total counts. Zero is valid. No change to binding, discovery authorization, journals, or existing describe_tools output.

Acceptance: exact budget bound; every namespace has a summary; fair representation when entries fit; deterministic insertion-order-independent output; no incomplete schema/type/example block; invalid limits and namespace names rejected; existing golden descriptions unchanged; public export and usage docs. Measure rendered context size, not a claimed runtime speedup.

Source idea: https://github.com/anomalyco/opencode/blob/dev/packages/codemode/src/tool-runtime.ts#L449
