# Issue notes and research takeaways

Research notes and prepared issue bodies. They propose work; they do not change code, and nothing here is
a commitment or part of the public contract.

| Note | Subject |
| --- | --- |
| [OpenCode follow-ups](opencode-validation.md) | Prepared issue bodies ([tool catalog](opencode-tool-catalog.md), [async lifecycle](opencode-async-lifecycle.md), [output boundaries](opencode-output-boundaries.md)) and how the implementation was validated. |
| [Goose Code Mode takeaways](goose-codemode-takeaways.md) | goose's code-execution mode (and its pctx engine) against pydeno's agent surface: what pydeno already does, seven proposed changes with priority and risk, and what is not worth copying. |
| [Omnigent takeaways](omnigent-takeaways.md) | omnigent-ai/omnigent: an `http_fetch` address gap, a tool-call hook design, loop detection, and what not to copy. |
| [TrueForge takeaways](trueforge-takeaways.md) | Agent harness (TypeScript, process sandbox): result source, tool annotations, oversized-result preview, per-call tool deadline, backend contract suites. No V8, WASM or Deno content. |
| [Crush takeaways](crush-takeaways.md) | charmbracelet/crush (Go terminal agent): per-call policy hooks, head+tail output retention, telling the model its limits, denial semantics, repeat guards, background jobs, CI hygiene. |
