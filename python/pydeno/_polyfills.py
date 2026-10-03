"""Opt-in browser basics for guests that run popular libraries.

A bare isolate has no `setTimeout`, `TextEncoder`, `btoa`, `Blob`... so libraries that assume a
browser or Node fail on the first use. These are pure JavaScript with no host access: nothing here
reaches the clock, the network or the filesystem.

    RuntimeConfig(bootstrap=WEB_POLYFILLS)

Design points that matter for a sandbox:

* Timers run on *virtual time*: `setTimeout(f, 5000)` does not wait, it runs after everything due
  earlier, in order. A guest therefore cannot sleep, and `performance.now()` is that virtual
  counter, not a clock: there is no timing side channel in it.
* Timers drain through the microtask queue, one per turn, and at most `MAX_TIMER_FIRES` run in
  total, so a `setInterval` that never stops ends instead of spinning until the deadline.
* No `window`, `document` or `navigator`: defining them would make libraries pick their DOM code
  paths. `self` and `global` alias `globalThis`, which is all the common UMD preambles probe.
"""

WEB_POLYFILLS = r"""
(() => {
  const g = globalThis;
  const MAX_TIMER_FIRES = 100000;
  const define = (name, value) => {
    if (typeof g[name] === 'undefined') {
      Object.defineProperty(g, name, { value, writable: true, configurable: true });
    }
  };

  // ---- self / global ----------------------------------------------------------------------
  define('self', g);
  define('global', g);

  // ---- timers on virtual time -------------------------------------------------------------
  let now = 0, seq = 0, fires = 0, scheduled = false;
  const queue = [];
  const run = () => {
    scheduled = false;
    if (!queue.length) return;
    let best = 0;
    for (let i = 1; i < queue.length; i++) {
      const a = queue[i], b = queue[best];
      if (a.t < b.t || (a.t === b.t && a.id < b.id)) best = i;
    }
    const timer = queue.splice(best, 1)[0];
    if (++fires > MAX_TIMER_FIRES) { queue.length = 0; return; }
    if (timer.t > now) now = timer.t;
    if (timer.every !== null) { timer.t = now + timer.every; queue.push(timer); }
    try { timer.fn(...timer.args); } catch (e) {
      if (typeof console !== 'undefined') console.error('Uncaught (in timer):', e && e.message || e);
    }
    if (queue.length) pump();
  };
  const pump = () => { if (!scheduled) { scheduled = true; Promise.resolve().then(run); } };
  const add = (fn, ms, args, repeat) => {
    if (typeof fn !== 'function') return 0;
    const delay = Math.max(0, Number(ms) || 0);
    const timer = { id: ++seq, t: now + delay, fn, args, every: repeat ? Math.max(1, delay) : null };
    queue.push(timer);
    pump();
    return timer.id;
  };
  const clear = (id) => {
    const i = queue.findIndex((x) => x.id === id);
    if (i >= 0) queue.splice(i, 1);
  };
  define('setTimeout', (fn, ms, ...args) => add(fn, ms, args, false));
  define('setInterval', (fn, ms, ...args) => add(fn, ms, args, true));
  define('clearTimeout', clear);
  define('clearInterval', clear);
  define('performance', { now: () => now, timeOrigin: 0 });

  // ---- UTF-8 ------------------------------------------------------------------------------
  class TextEncoder {
    get encoding() { return 'utf-8'; }
    encode(input = '') {
      const s = String(input), out = [];
      for (let i = 0; i < s.length; i++) {
        let c = s.charCodeAt(i);
        if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
          const d = s.charCodeAt(i + 1);
          if (d >= 0xdc00 && d <= 0xdfff) { c = 0x10000 + ((c - 0xd800) << 10) + (d - 0xdc00); i++; }
        }
        if (c >= 0xd800 && c <= 0xdfff) c = 0xfffd;
        if (c < 0x80) out.push(c);
        else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
        else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
        else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
      }
      return Uint8Array.from(out);
    }
  }
  class TextDecoder {
    constructor(label = 'utf-8') { this.encoding = String(label).toLowerCase(); }
    decode(input) {
      if (input === undefined) return '';
      const b = input instanceof ArrayBuffer ? new Uint8Array(input)
        : new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
      const parts = [];
      let s = '';
      for (let i = 0; i < b.length;) {
        const c = b[i];
        let cp = 0xfffd, n = 1;
        if (c < 0x80) cp = c;
        else if (c >= 0xc2 && c < 0xe0 && (b[i + 1] & 0xc0) === 0x80) { cp = ((c & 31) << 6) | (b[i + 1] & 63); n = 2; }
        else if (c >= 0xe0 && c < 0xf0 && (b[i + 1] & 0xc0) === 0x80 && (b[i + 2] & 0xc0) === 0x80) {
          cp = ((c & 15) << 12) | ((b[i + 1] & 63) << 6) | (b[i + 2] & 63); n = 3;
          if (cp < 0x800 || (cp >= 0xd800 && cp <= 0xdfff)) cp = 0xfffd;
        } else if (c >= 0xf0 && c < 0xf5 && (b[i + 1] & 0xc0) === 0x80 && (b[i + 2] & 0xc0) === 0x80 && (b[i + 3] & 0xc0) === 0x80) {
          cp = ((c & 7) << 18) | ((b[i + 1] & 63) << 12) | ((b[i + 2] & 63) << 6) | (b[i + 3] & 63); n = 4;
          if (cp < 0x10000 || cp > 0x10ffff) cp = 0xfffd;
        }
        s += String.fromCodePoint(cp);
        i += n;
        if (s.length > 8192) { parts.push(s); s = ''; }
      }
      parts.push(s);
      return parts.join('');
    }
  }
  define('TextEncoder', TextEncoder);
  define('TextDecoder', TextDecoder);

  // ---- base64 -----------------------------------------------------------------------------
  const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  define('btoa', (input) => {
    const s = String(input);
    let out = '';
    for (let i = 0; i < s.length; i += 3) {
      const a = s.charCodeAt(i), b = s.charCodeAt(i + 1), c = s.charCodeAt(i + 2);
      if (a > 255 || b > 255 || c > 255) throw new Error('InvalidCharacterError');
      const n = (a << 16) | ((b || 0) << 8) | (c || 0);
      out += B64[n >> 18] + B64[(n >> 12) & 63]
        + (i + 1 < s.length ? B64[(n >> 6) & 63] : '=') + (i + 2 < s.length ? B64[n & 63] : '=');
    }
    return out;
  });
  define('atob', (input) => {
    const s = String(input).replace(/[\t\n\f\r ]/g, '').replace(/=+$/, '');
    if (/[^A-Za-z0-9+/]/.test(s) || s.length % 4 === 1) throw new Error('InvalidCharacterError');
    let out = '';
    for (let i = 0; i < s.length; i += 4) {
      const n = (B64.indexOf(s[i]) << 18) | (B64.indexOf(s[i + 1]) << 12)
        | ((i + 2 < s.length ? B64.indexOf(s[i + 2]) : 0) << 6) | (i + 3 < s.length ? B64.indexOf(s[i + 3]) : 0);
      out += String.fromCharCode((n >> 16) & 255);
      if (i + 2 < s.length) out += String.fromCharCode((n >> 8) & 255);
      if (i + 3 < s.length) out += String.fromCharCode(n & 255);
    }
    return out;
  });

  // ---- Blob / FileReader / AbortController / structuredClone ------------------------------
  class Blob {
    constructor(parts = [], options = {}) {
      const chunks = parts.map((p) => typeof p === 'string' ? new TextEncoder().encode(p)
        : p instanceof ArrayBuffer ? new Uint8Array(p)
        : p instanceof Blob ? p._bytes
        : new Uint8Array(p.buffer, p.byteOffset, p.byteLength));
      const size = chunks.reduce((n, c) => n + c.length, 0);
      this._bytes = new Uint8Array(size);
      let at = 0;
      for (const c of chunks) { this._bytes.set(c, at); at += c.length; }
      this.type = String(options.type || '').toLowerCase();
    }
    get size() { return this._bytes.length; }
    arrayBuffer() { return Promise.resolve(this._bytes.slice().buffer); }
    text() { return Promise.resolve(new TextDecoder().decode(this._bytes)); }
    slice(start, end, type) { const b = new Blob([this._bytes.slice(start, end)]); b.type = type || ''; return b; }
  }
  define('Blob', Blob);
  class FileReader {
    constructor() { this.result = null; this.onload = null; this.onloadend = null; this.onerror = null; }
    _finish(value) {
      add(() => {
        this.result = value;
        if (this.onload) this.onload({ target: this });
        if (this.onloadend) this.onloadend({ target: this });
      }, 0, [], false);
    }
    readAsArrayBuffer(blob) { this._finish(blob._bytes.slice().buffer); }
    readAsText(blob) { this._finish(new TextDecoder().decode(blob._bytes)); }
    readAsDataURL(blob) {
      let s = '';
      for (let i = 0; i < blob._bytes.length; i++) s += String.fromCharCode(blob._bytes[i]);
      this._finish('data:' + (blob.type || 'application/octet-stream') + ';base64,' + btoa(s));
    }
  }
  define('FileReader', FileReader);
  class Event {
    constructor(type, init = {}) {
      this.type = String(type); this.defaultPrevented = false; this.target = null;
      this.cancelable = !!init.cancelable; this.bubbles = !!init.bubbles;
    }
    preventDefault() { if (this.cancelable) this.defaultPrevented = true; }
    stopPropagation() {}
  }
  class EventTarget {
    constructor() { Object.defineProperty(this, '_listeners', { value: new Map() }); }
    addEventListener(type, fn) {
      if (!fn) return;
      const list = this._listeners.get(type) || [];
      if (!list.includes(fn)) list.push(fn);
      this._listeners.set(type, list);
    }
    removeEventListener(type, fn) {
      const list = this._listeners.get(type);
      if (list) this._listeners.set(type, list.filter((x) => x !== fn));
    }
    dispatchEvent(event) {
      event.target = this;
      for (const fn of [...(this._listeners.get(event.type) || [])]) {
        if (typeof fn === 'function') fn.call(this, event); else if (fn && fn.handleEvent) fn.handleEvent(event);
      }
      return !event.defaultPrevented;
    }
  }
  define('Event', Event);
  define('EventTarget', EventTarget);
  // three.js (its loaders) reaches for AbortController at load time.
  class AbortSignal extends EventTarget {
    constructor() { super(); this.aborted = false; this.reason = undefined; }
    throwIfAborted() { if (this.aborted) throw this.reason; }
  }
  class AbortController {
    constructor() { this.signal = new AbortSignal(); }
    abort(reason) {
      const s = this.signal;
      if (s.aborted) return;
      s.aborted = true;
      s.reason = reason === undefined ? new Error('AbortError') : reason;
      s.dispatchEvent(new Event('abort'));
    }
  }
  define('AbortSignal', AbortSignal);
  define('AbortController', AbortController);
  // dagre (graphlib) clones with it. Plain data, Maps/Sets, dates and typed arrays; no cycles
  // beyond what `seen` handles, and no functions (a real structuredClone throws on those too).
  const clone = (v, seen) => {
    if (v === null || typeof v !== 'object') return v;
    if (seen.has(v)) return seen.get(v);
    let out;
    if (v instanceof Date) out = new Date(v.getTime());
    else if (v instanceof RegExp) out = new RegExp(v.source, v.flags);
    else if (v instanceof ArrayBuffer) out = v.slice(0);
    else if (ArrayBuffer.isView(v)) out = new v.constructor(v.buffer.slice(0), v.byteOffset, v.length);
    else if (v instanceof Map) { out = new Map(); seen.set(v, out); v.forEach((x, k) => out.set(clone(k, seen), clone(x, seen))); return out; }
    else if (v instanceof Set) { out = new Set(); seen.set(v, out); v.forEach((x) => out.add(clone(x, seen))); return out; }
    else if (Array.isArray(v)) { out = []; seen.set(v, out); v.forEach((x, i) => { out[i] = clone(x, seen); }); return out; }
    else { out = {}; seen.set(v, out); for (const k of Object.keys(v)) out[k] = clone(v[k], seen); return out; }
    seen.set(v, out);
    return out;
  };
  define('structuredClone', (v) => clone(v, new Map()));
})();
"""
