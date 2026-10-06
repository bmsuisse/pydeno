// The two prototypes only an `Intl.Segmenter` instance reaches (what `segment()` returns, and the
// iterator over that), as an array. The bridge in `ops.rs` needs them to refuse a namespace
// binding onto a built-in. It is an expression with no free variables but the standard globals, so
// the same text runs in two places:
//
//   * `build.rs`, once, while the startup snapshot is made, so the result is part of the snapshot;
//   * the bridge, when the runtime was not restored from that snapshot (a user snapshot, or V8
//     flags that rule the built-in one out).
//
// Getting them costs about 7 ms, nearly all of it ICU start-up for the first `Intl` object of the
// process, which is why they are worth a snapshot. Everything else the bridge collects is made
// by the runtime itself (the call-site prototype, WebAssembly) and cannot come from a snapshot.
(function () {
  "use strict";
  if (typeof Intl !== "object" || typeof Intl.Segmenter !== "function") {
    return [];
  }
  const GetPrototypeOf = Object.getPrototypeOf;
  const segments = new Intl.Segmenter().segment("a");
  return [GetPrototypeOf(segments), GetPrototypeOf(segments[Symbol.iterator]())];
})()
