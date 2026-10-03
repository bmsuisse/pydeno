# Vendored public libraries

Real, popular, third-party JavaScript used by `tests/test_isolated_libraries.py` to prove that
code people actually run still works **inside the OS sandbox with the hardened defaults**
(`IsolatedRuntime`: jitless V8, no `SharedArrayBuffer`/`Atomics`/`WeakRef`/`FinalizationRegistry`,
seccomp + Landlock/Seatbelt). Hermetic by design: no network, nothing is fetched at test time.

| File | Package | Version | Licence | How it was made |
|---|---|---|---|---|
| `vega-6.4.0.min.js` | `vega` | 6.4.0 | BSD-3-Clause | `build/vega.min.js` from the npm tarball, **unmodified** |
| `vega-lite-6.4.3.min.js` | `vega-lite` | 6.4.3 | BSD-3-Clause | `build/vega-lite.min.js`, **unmodified** |
| `three-0.180.0-gltf.bundle.js` | `three` + `GLTFExporter` | 0.180.0 | MIT | esbuild IIFE (see below) |
| `dagre.bundle.js` | `@dagrejs/dagre` | 3.1.1 | MIT | esbuild IIFE (see below) |

The pptxgenjs bundle lives in `../pptxgenjs/` (see its README).

Entry sources (each bundle is a one-line import that sets a global):

    three:      import * as THREE from "three"; import {GLTFExporter} from "three/examples/jsm/exporters/GLTFExporter.js";
                globalThis.THREE = THREE; globalThis.GLTFExporter = GLTFExporter;
    dagre:      import * as d from "@dagrejs/dagre"; globalThis.dagre = d;

Rebuild an esbuild bundle:

    npm install three@0.180.0 esbuild
    npx esbuild entry.js --bundle --format=iife --platform=neutral \
        --target=es2020 --minify --legal-comments=inline --outfile=three-0.180.0-gltf.bundle.js

(the others use `--platform=browser --define:process.env.NODE_ENV='"production"'`). Each file's
SHA-256 is pinned in the test, so a silent change to vendored code fails loudly. Licence texts
are in `LICENSES/`.

Libraries that were also checked by hand under the sandbox (not vendored, too large): lodash,
date-fns, markdown-it, highlight.js, xlsx, d3, echarts (SSR to SVG), mathjs, katex, handlebars,
zod, ajv, yaml, fuse.js, qrcode-generator, jsPDF, Tailwind CSS v4 (`compile`), immer.
mermaid and Chart.js load but need a DOM or canvas, which a sandbox does not provide.
