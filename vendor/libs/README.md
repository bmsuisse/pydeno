# Vendored public libraries

Real, popular, third-party JavaScript used by `tests/test_isolated_libraries.py` to prove that
code people actually run still works **inside the OS sandbox with the hardened defaults**
(`IsolatedRuntime`: jitless V8, no `SharedArrayBuffer`/`Atomics`/`WeakRef`/`FinalizationRegistry`,
seccomp + Landlock/Seatbelt). Hermetic by design: no network, nothing is fetched at test time.

| File | Package | Version | Licence | How it was made |
|---|---|---|---|---|
| `vega-6.4.0.min.js` | `vega` | 6.4.0 | BSD-3-Clause | `build/vega.min.js` from the npm tarball, **unmodified** |
| `vega-lite-6.4.3.min.js` | `vega-lite` | 6.4.3 | BSD-3-Clause | `build/vega-lite.min.js`, **unmodified** |
| `echarts-6.1.0.min.js` | `echarts` | 6.1.0 | Apache-2.0 (+ NOTICE) | `dist/echarts.min.js` from the npm tarball, **unmodified**; used by `examples/monty_echarts_dashboard.py` (SSR to SVG, no DOM) |
| `three-0.180.0-gltf.bundle.js` | `three` + `GLTFExporter` | 0.180.0 | MIT | esbuild IIFE (see below) |
| `dagre.bundle.js` | `@dagrejs/dagre` | 3.1.1 | MIT | esbuild IIFE (see below) |
| `turf-7.4.0.bundle.js` | `@turf/turf` (whole package, 527 KB minified) | 7.4.0 | MIT (+ bundled third-party notices) | esbuild IIFE, `--platform=browser` (see below) |
| `d3-force-3.0.0-delaunay-6.0.4.bundle.js` | `d3-force` 3.0.0, `d3-delaunay` 6.0.4 (+ `delaunator` 5.1.0, `robust-predicates` 3.0.3), `d3-hierarchy` 3.1.2, `d3-scale` 4.0.2, `d3-shape` 3.2.0, `d3-array` 3.2.4, `d3-scale-chromatic` 3.1.0 | see left | ISC (robust-predicates: public domain) | esbuild IIFE (see below); `--platform=browser`, no DOM needed |

The pptxgenjs bundle lives in `../pptxgenjs/` (see its README).

Entry sources (each bundle is a one-line import that sets a global):

    three:      import * as THREE from "three"; import {GLTFExporter} from "three/examples/jsm/exporters/GLTFExporter.js";
                globalThis.THREE = THREE; globalThis.GLTFExporter = GLTFExporter;
    dagre:      import * as d from "@dagrejs/dagre"; globalThis.dagre = d;
    turf:       import * as turf from "@turf/turf"; globalThis.turf = turf;
    d3:         import * as force from "d3-force"; import * as delaunay from "d3-delaunay"; import * as hierarchy from "d3-hierarchy";
                import * as scale from "d3-scale"; import * as shape from "d3-shape"; import * as array from "d3-array"; import * as chromatic from "d3-scale-chromatic";
                globalThis.d3 = {...array, ...force, ...delaunay, ...hierarchy, ...scale, ...shape, ...chromatic};

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

## Pinned sizes and SHA-256

Hosts can pin these bytes without rebuilding. The bundles stay byte-identical within a minor
release (a change is a changelog entry), and the tests pin the same hashes.

| File | Bytes | SHA-256 |
|---|---:|---|
| `d3-force-3.0.0-delaunay-6.0.4.bundle.js` | 175512 | `67e190242161066fea190c201ea2f97ea4d3d97fb1ba9f2f577f33d5dc5b97f7` |
| `dagre.bundle.js` | 48411 | `ca109f634a32870d6865e6cb01702a3c8cca68eeb3dccde871aa031ef4b2dbd0` |
| `echarts-6.1.0.min.js` | 1121883 | `b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0` |
| `three-0.180.0-gltf.bundle.js` | 738530 | `b3faa3da4cf40d0fad9883002324ed35bfb0a57cbc4fdb1584f1df8065ba061a` |
| `turf-7.4.0.bundle.js` | 539760 | `ab93309f52566b6cd998200485d4825be1c434c5c8f81a3e18dde0a92dd63940` |
| `vega-6.4.0.min.js` | 521123 | `8f6a3587cf8d4f42c7e08120e3eb05d067e746d554e39d2dcf52acc0bd5ba28f` |
| `vega-lite-6.4.3.min.js` | 250845 | `35a9821df838825b05a6a73e9414b58747a1b18321583858ed903c66393a5c7e` |
