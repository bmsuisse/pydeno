# Vendored public libraries

Real, popular, third-party JavaScript used by `tests/test_isolated_libraries.py` to prove that
code people actually run still works **inside the OS sandbox with the hardened defaults**
(`IsolatedRuntime`: jitless V8, no `SharedArrayBuffer`/`Atomics`/`WeakRef`/`FinalizationRegistry`,
seccomp + Landlock/Seatbelt). Hermetic by design: no network, nothing is fetched at test time.

| File | Package | Version | Licence | How it was made |
|---|---|---|---|---|
| `vega-6.4.0.min.js` | `vega` | 6.4.0 | BSD-3-Clause | `build/vega.min.js` from the npm tarball, **unmodified** |
| `vega-lite-6.4.3.min.js` | `vega-lite` | 6.4.3 | BSD-3-Clause | `build/vega-lite.min.js`, **unmodified** |
| `vega-interpreter-2.3.2.bundle.js` | `vega-interpreter` | 2.3.2 | BSD-3-Clause | esbuild IIFE of the npm package's `build/vega-interpreter.js` (unmodified source), its `vega-util` import bound to the global `vega` (see below); Vega's CSP-safe expression interpreter, for `strict_eval=True` |
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
    vega-interpreter:
                import {expressionInterpreter} from "vega-interpreter"; globalThis.vega.expressionInterpreter = expressionInterpreter;
                (with vega-util aliased to a two-line module that re-exports `ascending`, `isString` and
                `DisallowedObjectProperties` from `globalThis.vega`, so it shares the vega-util that the
                vendored Vega 6.4.0 already carries instead of bundling a second copy)
    d3:         import * as force from "d3-force"; import * as delaunay from "d3-delaunay"; import * as hierarchy from "d3-hierarchy";
                import * as scale from "d3-scale"; import * as shape from "d3-shape"; import * as array from "d3-array"; import * as chromatic from "d3-scale-chromatic";
                globalThis.d3 = {...array, ...force, ...delaunay, ...hierarchy, ...scale, ...shape, ...chromatic};

Rebuild an esbuild bundle:

    npm install three@0.180.0 esbuild
    npx esbuild entry.js --bundle --format=iife --platform=neutral \
        --target=es2020 --minify --legal-comments=inline --outfile=three-0.180.0-gltf.bundle.js

(the others use `--platform=browser --define:process.env.NODE_ENV='"production"'`).

How `vega-interpreter-2.3.2.bundle.js` was made (reproducible: rebuilding gives the same bytes):

    # npm registry tarball https://registry.npmjs.org/vega-interpreter/-/vega-interpreter-2.3.2.tgz
    #   sha1 6374066a76844de22763937728acbc0261f2ebf1 (the registry's `shasum`)
    #   sha512 JDAoi3taFcCDLujZG84TNNUXdkAZ5WsSHssx8lWVYaxb9Slsjk7v7PtRIYXpSlUwLaKGRBqoJ9KDs36Z0eMEIw==
    #   (the registry's `integrity`); package.json: "license": "BSD-3-Clause", devDependency vega 6.4.0
    #   build/vega-interpreter.js sha256 c0bdb735a63f9276f136e8d90d7fdf5d8e9df8031eb069048fc9975c876fd7f9
    npm install vega-interpreter@2.3.2 esbuild@0.25.10
    printf '%s\n' 'const v = globalThis.vega;' \
      'export const ascending = v.ascending, isString = v.isString, DisallowedObjectProperties = v.DisallowedObjectProperties;' \
      > vega-util-global.mjs
    echo 'import {expressionInterpreter} from "vega-interpreter"; globalThis.vega.expressionInterpreter = expressionInterpreter;' > entry.mjs
    npx esbuild entry.mjs --bundle --format=iife --platform=neutral --target=es2020 --minify \
        --legal-comments=inline --alias:vega-util=$PWD/vega-util-global.mjs \
        --outfile=vega-interpreter-2.3.2.bundle.js

Load it after `vega-6.4.0.min.js`, with `WEB_POLYFILLS` (it reads `setTimeout` when it loads), then
`new vega.View(vega.parse(spec, null, {ast: true}), {expr: vega.expressionInterpreter, ...})`. Its
licence (the npm package's `LICENSE`) is `LICENSES/vega-interpreter.LICENSE`. Each file's
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
| `vega-interpreter-2.3.2.bundle.js` | 5098 | `54d2c534de8f0b35e29db6170a4776e666847c9b88c5fd15d56489575c89abdb` |
| `vega-lite-6.4.3.min.js` | 250845 | `35a9821df838825b05a6a73e9414b58747a1b18321583858ed903c66393a5c7e` |
