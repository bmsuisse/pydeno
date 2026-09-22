/*
 * Host-supplied browser globals for running a real npm browser/UMD bundle
 * inside a bare pydeno V8 isolate.
 *
 * This file is HOST code, not vendored third-party code, and it is injected
 * SEPARATELY -- the bundle itself is never patched. Keeping it as its own
 * reviewable artifact is deliberate: the polyfill surface is the thing that
 * makes large real-world bundles work, so a change here is a change to a
 * load-bearing contract and should show up in a diff on its own.
 *
 * `tests/test_vendored_bundle_execution.py` asserts each section below both
 * behaves correctly and is actually needed (or, where it is not needed,
 * says so explicitly rather than pretending).
 */

/* ------------------------------------------------------------------------
 * 1. UMD global branch.
 *
 * pptxgen.bundle.js opens with the standard UMD preamble:
 *
 *   ("undefined"!=typeof window ? window
 *     : "undefined"!=typeof global ? global
 *     : "undefined"!=typeof self ? self
 *     : this).JSZip = e()
 *
 * A bare V8 isolate has no window/global/self, so the expression falls
 * through to `this`.
 *
 * MEASURED, pptxgenjs 4.0.1 on pydeno: pydeno's `eval` runs the script as a
 * sloppy-mode classic script whose top-level `this` IS globalThis, so the
 * fallback already lands on the real global object and the UMD assignment
 * works WITHOUT this section. It is kept for two reasons: (a) it removes the
 * dependency on that `this` binding, which is an implementation detail of
 * how the host evaluates the script rather than a documented guarantee, and
 * (b) other bundles branch on `typeof global`/`typeof self` for real feature
 * detection, not just for the assignment target.
 *
 * But this section is NOT inert, and that is the surprising part: it changes
 * which object the bundle's `setimmediate`/`immediate` shims bind their
 * scheduler to. Inside the bundle:
 *
 *   (r = "undefined"==typeof self ? void 0===e ? this : e : self).setImmediate || ...
 *
 * so `self`/`global` being defined picks a different `r`. With no `process`,
 * `MessageChannel`, `MutationObserver` or `document` in the isolate, every
 * one of those code paths bottoms out in `setTimeout` -- which is why this
 * section and section 2 below are COUPLED. See section 2 for the measured
 * consequence.
 * --------------------------------------------------------------------- */
globalThis.global = globalThis;
globalThis.self = globalThis;

/* ------------------------------------------------------------------------
 * 2. setTimeout / clearTimeout -- THE load-bearing polyfill.
 *
 * pptxgenjs writes its .pptx through JSZip, whose async pipeline advances
 * itself through `setImmediate` (JSZip's `utils.delay`), and the bundled
 * `setimmediate`/`immediate` shims implement that on top of `setTimeout`
 * once they find no `process`, `MessageChannel`, `MutationObserver` or
 * `document`. So `setTimeout` is the engine that drives `write()` to
 * completion, and without one that both *defers* and *actually fires*,
 * `pres.write()` returns a promise that never settles.
 *
 * MEASURED, pptxgenjs 4.0.1 on pydeno, building the same deck:
 *
 *                              | section 1 absent | section 1 present
 *   setTimeout = f => {}       |      HANGS       |      HANGS
 *   setTimeout = f => f()      |      works       |      HANGS
 *   Promise.resolve().then(f)  |      works       |      works
 *
 * The no-op row is obvious. The middle row is the trap, and it is the shim
 * most people reach for first: a synchronous `setTimeout` invokes the
 * callback inline, re-entering the pipeline before the state it is about to
 * advance has been committed, and the resume is lost -- but ONLY on the
 * scheduler path that `self`/`global` being defined selects. That is why the
 * synchronous shim can look fine in a minimal setup and then hang the moment
 * someone adds a `global` polyfill for an unrelated bundle.
 *
 * The bottom row is the only one that is correct independent of the rest of
 * the polyfill set. `Promise.resolve().then()` puts the callback on the
 * microtask queue, which is exactly what pydeno's `eval_async` promise-polling
 * loop drains, so the continuation runs on a later turn as JSZip expects.
 * Do not "simplify" it.
 *
 * Delays are intentionally ignored (every callback lands on the next
 * microtask regardless of its requested delay). That is fine for a
 * document-generation pipeline, which uses setTimeout only to yield, and it
 * keeps deck generation fast and deterministic. Do not reuse this shim for
 * guest code that needs real wall-clock timers.
 * --------------------------------------------------------------------- */
(function () {
  let nextId = 1;
  const cancelled = new Set();

  globalThis.setTimeout = function (fn, _delay, ...args) {
    const id = nextId++;
    Promise.resolve().then(function () {
      if (cancelled.has(id)) {
        cancelled.delete(id);
        return;
      }
      fn(...args);
    });
    return id;
  };

  globalThis.clearTimeout = function (id) {
    cancelled.add(id);
  };

  // Same contract; some bundles feature-detect these alongside setTimeout.
  // Note this is NOT a repeating interval -- it fires once, like setTimeout.
  globalThis.setInterval = globalThis.setTimeout;
  globalThis.clearInterval = globalThis.clearTimeout;
})();

/* ------------------------------------------------------------------------
 * 3. atob / btoa.
 *
 * V8 has no browser base64 builtins; they live in the HTML spec, not in
 * ECMAScript.
 *
 * MEASURED, pptxgenjs 4.0.1 on pydeno: the deck in
 * `examples/pptxgenjs_presentation.py` -- including `addImage({data:
 * "image/png;base64,..."})` and `write({outputType: "base64"})` -- produces
 * byte-identical output WITHOUT this section, because JSZip carries its own
 * base64 codec and never delegates to the host's. So this is not required
 * for pptxgenjs today.
 *
 * It is kept because it is a genuine gap in the isolate's global surface
 * that other document bundles (exceljs' base64 output path, for one) do hit,
 * and because a future pptxgenjs could reasonably start using the platform
 * builtins. The implementation is spec-correct, not merely "good enough":
 * it round-trips arbitrary binary strings, pads properly, and rejects
 * out-of-range code units.
 * --------------------------------------------------------------------- */
(function () {
  const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

  globalThis.btoa = function (input) {
    const str = String(input);
    let out = "";
    for (let i = 0; i < str.length; i += 3) {
      const c0 = str.charCodeAt(i);
      const c1 = str.charCodeAt(i + 1);
      const c2 = str.charCodeAt(i + 2);
      if (c0 > 0xff || c1 > 0xff || c2 > 0xff) {
        throw new Error("btoa: invalid character (code unit > 0xFF)");
      }
      const n = (c0 << 16) | ((isNaN(c1) ? 0 : c1) << 8) | (isNaN(c2) ? 0 : c2);
      out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63];
      out += isNaN(c1) ? "=" : B64[(n >> 6) & 63];
      out += isNaN(c2) ? "=" : B64[n & 63];
    }
    return out;
  };

  globalThis.atob = function (input) {
    const str = String(input).replace(/[=\s]+$/, "");
    let out = "";
    let bits = 0;
    let acc = 0;
    for (let i = 0; i < str.length; i++) {
      const v = B64.indexOf(str[i]);
      if (v < 0) continue;
      acc = (acc << 6) | v;
      bits += 6;
      if (bits >= 8) {
        bits -= 8;
        out += String.fromCharCode((acc >> bits) & 0xff);
      }
    }
    return out;
  };
})();
