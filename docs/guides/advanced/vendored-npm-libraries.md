# Running vendored npm libraries safely

`pydeno` gives sandboxed JS a real V8 engine but no Node.js, no `require()`,
no filesystem, and no network by default. That's exactly the isolation you
want for LLM-generated code -- but a lot of genuinely useful JS lives in npm
packages, not in code the model writes from scratch. This page covers a
pattern for using some of that npm ecosystem *without* weakening the sandbox:
the **host** pre-loads a specific, versioned browser bundle it chooses, and
the guest JS only ever gets to call the API that bundle exposes.

## What makes a library a good candidate

Not every npm package works this way. A library is a good fit when:

- It ships a real, dependency-free **browser or UMD build** -- check its
  package's `dist/` folder, or fetch `https://unpkg.com/<pkg>/dist/...` /
  `https://cdn.jsdelivr.net/npm/<pkg>/dist/...` and look for an IIFE that
  assigns a global (`window.X = ...`), not a bundle that ends in a bare
  `export { ... }` (that's an ES module and needs a build-time rewrite --
  see the `docx` case below).
- Its **core functionality doesn't depend on real `fs`, real network I/O,
  or a DOM**. Document/data-generation libraries -- slide decks, PDFs,
  spreadsheets, schema validation -- tend to qualify, because their job is
  "take data in, produce bytes out." General web-app UI libraries usually
  don't, because they assume a real DOM to render into.
- It can produce its output as an in-memory buffer (base64 string,
  `Uint8Array`, `ArrayBuffer`) rather than only via `fs.writeFile`. Several
  libraries below support both a Node file-writing path and a
  buffer-output method (`.write("base64")`, `.saveAsBase64()`,
  `.writeBuffer()`) -- always use the buffer path.

## The polyfill pattern

A browser bundle typically assumes a handful of browser globals that a bare
V8 isolate doesn't provide. In practice the set needed is small and
library-specific:

```python
import pathlib
from pydeno import Runtime

polyfills = pathlib.Path("vendor/pptxgenjs/polyfills.js").read_text()

with Runtime() as runtime:
    runtime.eval(polyfills)          # host globals, injected separately
    runtime.eval(bundle_source)      # the vetted, versioned bundle text
    result = await runtime.eval_async(js_that_calls_the_library_api)
```

The host decides what `bundle_source` is (a file it ships, or a pinned URL it
fetches once) -- the guest JS never chooses what gets loaded. The polyfills go
in as a **separate** `eval`; the bundle itself is never patched, which is what
lets you assert it is byte-identical to the published tarball.

### `setTimeout` is the one that will bite you

!!! warning "A synchronous `setTimeout` shim is not safe, even when it works"

    The obvious shim -- `setTimeout = fn => { fn(); return 0 }` -- is wrong,
    and worse, it is *intermittently* wrong. Measured against pptxgenjs 4.0.1
    building the same deck:

    | `setTimeout` shim | `global`/`self` **absent** | `global`/`self` **present** |
    |---|---|---|
    | `fn => {}` (no-op) | hangs | hangs |
    | `fn => fn()` (synchronous) | **works** | **hangs** |
    | `Promise.resolve().then(fn)` | works | works |

    pptxgenjs writes through JSZip, which advances its async pipeline with
    `setImmediate`; the bundled `setimmediate`/`immediate` shims implement
    that on top of `setTimeout` once they find no `process`,
    `MessageChannel`, `MutationObserver` or `document`. Crucially they pick
    the object they bind that scheduler to with `typeof self` / `typeof
    global` probes, so **the polyfills are coupled**: a synchronous shim can
    pass every test you write and then hang the moment someone adds a
    `global` polyfill for an unrelated bundle.

    Back `setTimeout` with `Promise.resolve().then()`. It puts the callback on
    the microtask queue, which is what pydeno's `eval_async` promise-polling
    loop drains, and it is the only option that is correct independent of the
    rest of the polyfill set. A no-op or synchronous shim leaves
    `pres.write()`'s promise pending forever -- and with no `timeout=`
    argument that is an unbounded hang, not an error.

`vendor/pptxgenjs/polyfills.js` is the maintained version of that surface,
shared by the examples and by the regression test, with the reasoning for each
of the three sections inline.

## Verified examples

`examples/vendored_npm_libraries.py` runs this pattern end-to-end for two
libraries, fetching each bundle from a pinned CDN URL, and checks the output
is structurally real (a valid OOXML zip / a valid PDF), not just "didn't
throw". `examples/pptxgenjs_presentation.py` goes considerably further for
pptxgenjs specifically -- see [Verifying it properly](#verifying-it-properly)
below.

| Library | Real browser/UMD build? | Polyfills needed | Result |
|---|---|---|---|
| [`pptxgenjs`](https://www.npmjs.com/package/pptxgenjs) | Yes (`dist/pptxgen.bundle.js`) | **A deferring `setTimeout`/`clearTimeout`** (see the warning above). `atob`/`btoa` and `global`/`self` are supplied too, but measurably are not required by pptxgenjs 4.0.1 itself -- JSZip carries its own base64 codec | Builds a real multi-slide `.pptx` (title, table, three native charts incl. a combo chart on a secondary axis, an embedded image) |
| [`pdf-lib`](https://www.npmjs.com/package/pdf-lib) | Yes (`dist/pdf-lib.min.js`) | None | Builds a real multi-page `.pdf` |
| [`exceljs`](https://www.npmjs.com/package/exceljs) | Yes (`dist/exceljs.min.js`) | `setTimeout`/`clearTimeout`, `btoa` (for base64 output) | Builds a real `.xlsx` via `workbook.xlsx.writeBuffer()` (avoid its `fs`-based write paths) |
| [`zod`](https://www.npmjs.com/package/zod) | Yes (`lib/index.umd.js`) | None | Runs real schema validation, no I/O at all |
| [`docx`](https://www.npmjs.com/package/docx) (dolanmiu/docx) | **No** -- `build/index.js` is an ES module ending in `export { ... }`, not an IIFE | Same as above, plus a source rewrite of the trailing `export { A, B as C }` into `globalThis.docx = { A, C: B }` (valid because both are identifier-list grammars) | Document construction works after the rewrite, but `Packer.toBase64String()` never resolved its promise in testing -- likely something in its zip/compression pipeline expects a browser or Node capability this pattern doesn't provide. **Not currently recommended** without further investigation into that hang. |

### pptxgenjs and Anthropic's published `pptx` skill

Anthropic's [`pptx` skill](https://github.com/anthropics/skills/tree/main/skills/pptx)
has two halves, and only one of them is relevant to this pattern:

- **Deck creation** is a `pptxgenjs` script (the skill's `SKILL.md` documents
  the real API usage: `pres.layout`, `addSlide()`, `addText()`, `addTable()`,
  `addChart()`, `addImage()`, hex colors, speaker notes via `addNotes()`).
  This half runs unmodified inside a `pydeno.Runtime` with the two
  polyfills above -- verified here with a multi-slide deck containing a
  title slide, a table, a native chart, and an image, round-tripped through
  `python-pptx` to confirm it's a real, openable file.
- **Everything else in the skill is Python, not JS**, and has nothing to do
  with a JS sandbox: `scripts/thumbnail.py` (LibreOffice + Pillow rendering),
  `scripts/office/validate.py` (schema/relationship validation),
  `scripts/office/soffice.py` (LibreOffice conversion), `scripts/add_slide.py`
  and `scripts/clean.py` (raw OOXML XML manipulation). None of that runs in
  pydeno, and it isn't meant to -- don't confuse "the JS half of one
  skill works in a JS sandbox" with "the whole skill runs in pydeno."

## pptxgenjs API footguns

Two of these have nothing to do with pydeno -- they are pptxgenjs behaviours
that produce a file which passes a structural check and is broken for real
consumers. They are worth knowing before you blame the sandbox.

- **pptxgenjs mutates the options object you hand it**, in place: it
  normalises units and rewrites colour keys. Build a **fresh object literal
  for every `add*` call**. A shared `const opts = {...}` reused across calls
  silently leaks state from one shape into the next.
- **`secondaryValAxis`/`secondaryCatAxis` need BOTH `valAxes` and `catAxes`,
  with two entries each.** Supply only `valAxes` and you get two `<c:valAx>`
  against one `<c:catAx>`; PowerPoint discards the chart and reports the
  whole file as corrupt. The zip is still perfectly valid, so only an
  assertion on the chart XML catches it.

And three that only a renderer will show you:

- **Set both axis bounds.** `valAxisMaxVal` without a matching
  `valAxisMinVal` lets the renderer choose its own minimum, and an
  all-positive series ends up on an axis running negative.
- **Use literal six-digit hex in colour options, never a theme-scheme name.**
  A scheme name emits `<a:srgbClr val="accent1"/>`, which is invalid OOXML --
  `srgbClr` takes hex -- and LibreOffice draws it as bright cyan.
- **Set `legendColor` and the axis label colours explicitly.** Left unset,
  the legend can inherit a colour that renders near-invisible.

## Verifying it properly

"It didn't throw" is a very weak signal for a document generator, and so is
"it's a valid zip". `examples/pptxgenjs_presentation.py` builds a six-slide
deck from the pinned, vendored bundle and then validates in three escalating
steps:

1. **`zipfile`** -- integrity plus the expected OOXML parts (six slides,
   three chart parts, one media part, per-slide relationships).
2. **`python-pptx`** -- a real third-party consumer opens the file and reads
   back the expected slides, table, charts and picture. This catches broken
   relationships that a raw zip check sails past.
3. **LibreOffice + `pdftoppm`** -- renders every slide to PNG so you can
   *look* at it.

Step 3 matters more than it sounds. Every defect in the footgun list above
that mentions a renderer was found by looking at a PNG of a file that had
already passed steps 1 and 2. Validation proves a file is well-formed; only
rendering proves it looks right.

```bash
uv sync --group examples
uv run python examples/pptxgenjs_presentation.py
```

## Keeping it working

`tests/test_vendored_bundle_execution.py` is the permanent regression guard,
and it is fully hermetic: the bundle and the polyfills are committed under
`vendor/pptxgenjs/`, and validation uses only the standard library. There is
no network access and **no skip condition anywhere in it** -- it either runs
and passes, or runs and fails.

Vendoring 461 KB of minified text into the repo was a deliberate trade. The
alternatives were worse:

- **Fetch the bundle at test time.** This breaks hermeticity, and a test that
  is skipped when the network is unavailable is worse than no test: it looks
  like coverage. Pinning the bytes also means a failure is attributable to a
  change in pydeno rather than to npm republishing or CDN drift.
- **A synthetic stand-in** -- 460 KB of generated JS plus the polyfill
  surface, with pptxgenjs left as a documented example. This protects the
  cheap half of the property and misses the expensive half. The failure mode
  that actually bites lives inside JSZip's async pipeline and its coupling to
  `typeof self`/`typeof global`; no synthetic payload reproduces it.

The cost is modest either way: ~14 ms to evaluate the bundle, ~100 ms to build
the deck. What the test pins is the combination -- large-bundle evaluation,
the exact polyfill surface, and JSZip's async `write()` actually completing --
because a test that only proves "a small script runs" protects none of it.

## What this is NOT

This is **not** the same as giving guest JS a real `require()` or npm
resolver. There is no dynamic module resolution happening on the guest's
behalf, and guest code can never load anything the host didn't explicitly
provide -- the host chooses the exact bundle text (a specific version, often
pinned by URL or vendored on disk) before any guest code runs. A malicious
or buggy script running inside the sandbox cannot reach out and pull in an
arbitrary package; it can only call the API surface of whatever the host
already `eval`'d.

This is also **not yet a polished, automatic feature**. There is no
`Runtime(allow_modules=["pptxgenjs"])`-style API today -- the host has to
write its own polyfill script and its own `eval()`/`eval_async()` calls, as
shown above. Building a first-class "allow-listed vendored module" API (one
that ships known-good bundles + polyfill sets for a curated library list)
is real future work, not something this pattern already automates.
