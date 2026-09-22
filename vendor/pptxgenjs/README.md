# Vendored `pptxgenjs` browser bundle

`pptxgen.bundle.js` in this directory is the **unmodified** UMD build of
[`pptxgenjs`](https://www.npmjs.com/package/pptxgenjs), vendored so that
`tests/test_vendored_bundle_execution.py` can prove pydeno still executes a
real, large, third-party JavaScript bundle end-to-end **without touching the
network**.

| | |
|---|---|
| Package | `pptxgenjs` |
| Version | **4.0.1** |
| File | `dist/pptxgen.bundle.js` from `npm pack pptxgenjs@4.0.1` |
| Size | **460,889 bytes** |
| SHA-256 | `4fb9eac5cfefb213e2d8743c2b7151025f31bfb3f834c73c12062916daa0f3f8` |
| License | MIT (see `LICENSE`, © 2015-2022 Brent Ely) |
| Modifications | **none** — byte-identical to the published tarball |

`polyfills.js` is *our* code, not vendored code: the three host-supplied
browser globals the bundle needs. It is injected separately and the bundle is
never patched. See the comments in that file for which of the three are
actually load-bearing (measured, not assumed).

## Why this is vendored rather than fetched

pydeno's test suite is hermetic, and the regression being guarded — "a large
real-world bundle still evaluates, and JSZip's async `write()` still
completes" — is precisely the kind that a network-dependent test fails to
catch, because it gets skipped on the machines where it matters. Pinning the
exact bytes also means a failure is attributable to a change in pydeno rather
than to npm republishing or CDN drift.

The cost is modest: 461 KB of minified text (it is text, and compresses
well), against ~14 ms to evaluate and ~100 ms to build a six-slide deck.

## Refreshing to a newer pptxgenjs

```bash
npm pack pptxgenjs@<version>
tar xzf pptxgenjs-<version>.tgz
cp package/dist/pptxgen.bundle.js vendor/pptxgenjs/pptxgen.bundle.js
cp package/LICENSE                vendor/pptxgenjs/LICENSE
shasum -a 256 vendor/pptxgenjs/pptxgen.bundle.js
```

Then update the version, byte size and SHA-256 in the table above **and** the
matching constants in `tests/test_vendored_bundle_execution.py`. Those
constants are asserted, so a refresh that forgets to update them fails
loudly instead of silently changing what the suite tests.
