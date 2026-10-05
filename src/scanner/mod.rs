//! The static source scanner behind `check_source` / `SourcePolicy` / `static_gate`.
//!
//! A pure function: source text and options in, findings out. No callbacks into Python, no
//! threads, no Python objects kept. The Python binding copies the text into a `Vec<u32>` of code
//! points while it holds the GIL, then scans with the GIL released.
//!
//! The specification is the Python implementation (`pydeno._preflight_reference`): this port gives
//! identical findings (rules, offsets, order, caps), which `tests/test_scanner_differential.py`
//! checks against it. It works on code points, not bytes, because Python's offsets are code
//! points and a Python `str` may hold lone surrogates (which a Rust `String` cannot). Character
//! classes (`\w`, `\s`, `isdigit`, `isidentifier`) come from tables generated from CPython for
//! each Unicode version it ships (`unicode.rs`), so a word ends where Python's `re` says it does.
//!
//! Everything is iterative (no recursion), every look-around is bounded, and every loop advances,
//! so the scan is linear in the length of the text and cannot overflow the stack.

mod text;
mod tokens;
#[rustfmt::skip]
mod unicode;

use rustc_hash::FxHashMap as HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};

/// The most UTF-8 bytes the native scanner accepts (the gate's own cap). Checked before the text
/// is copied; a longer text is refused with `ValueError`.
pub const MAX_SOURCE_BYTES: usize = 16 * 1024 * 1024;

// Finding codes shared with `pydeno._preflight` (`_NATIVE_FINDINGS`). The policy rules:
pub const FORBIDDEN_IDENTIFIER: u8 = 1; // arg: index into the identifiers given
pub const FORBIDDEN_GLOBAL: u8 = 2; // arg: index into the globals given
pub const FORBIDDEN_DYNAMIC_IMPORT: u8 = 3;
pub const FORBIDDEN_EVAL: u8 = 4;
pub const FORBIDDEN_STRING_TIMER: u8 = 5; // arg: 0 setTimeout, 1 setInterval
pub const FORBIDDEN_FUNCTION_CONSTRUCTOR: u8 = 6;
pub const FORBIDDEN_WEBASSEMBLY: u8 = 7;
pub const FORBIDDEN_COMPUTED_GLOBAL_ACCESS: u8 = 8;
// The usability rules (`check_source` without a policy, or `include_preflight_rules`):
pub const GLOBAL_ALIAS_KEY: u8 = 20; // arg: 16 * global index + name index
pub const GLOBAL_ALIAS_NAME: u8 = 21; // arg: name index
pub const PROTO_STRING: u8 = 22;
pub const PROTO_NAME: u8 = 23;
pub const CONSTRUCTOR_CONSTRUCTOR: u8 = 24;
pub const STATIC_EXPORT: u8 = 25;
pub const DYNAMIC_IMPORT: u8 = 26;
pub const IMPORT_META: u8 = 27;
pub const STATIC_IMPORT: u8 = 28;
pub const NAMED: u8 = 29; // arg: name index (the rule is that name's)
pub const EVAL_CALL: u8 = 30;
pub const NEW_FUNCTION: u8 = 31;

/// `_GLOBALS` in the reference, in this order (indexes are part of `GLOBAL_ALIAS_KEY`).
pub const GLOBALS: [&str; 4] = ["globalThis", "self", "window", "global"];
/// `_NAMES` in the reference, in this order.
pub const NAMES: [&str; 7] = [
    "require",
    "fetch",
    "XMLHttpRequest",
    "WebSocket",
    "process",
    "Deno",
    "child_process",
];
/// What must follow each of `NAMES` (`None`: nothing).
const NAME_NEEDS: [Option<char>; 7] =
    [Some('('), Some('('), None, None, Some('.'), Some('.'), None];

/// The character classes of one Unicode version, as CPython computes them.
#[derive(Clone, Copy)]
pub struct Classes {
    shift: u32,
}

const ALNUM: u8 = 1;
const SPACE: u8 = 2;
const DIGIT: u8 = 4;
const IDENTIFIER: u8 = 8;

impl Classes {
    /// The classes of `version` (`unicodedata.unidata_version`), if the tables have it.
    pub fn for_version(version: &str) -> Option<Self> {
        unicode::VERSIONS
            .iter()
            .position(|v| *v == version)
            .map(|k| Classes {
                shift: 4 * k as u32,
            })
    }

    /// The Unicode versions the tables cover.
    #[cfg(test)]
    pub fn versions() -> &'static [&'static str] {
        &unicode::VERSIONS
    }

    #[inline]
    fn flags(self, c: u32) -> u8 {
        if c < 128 {
            return unicode::ASCII[c as usize];
        }
        let k = unicode::STARTS.partition_point(|&s| s <= c);
        match k.checked_sub(1).and_then(|k| unicode::FLAGS.get(k)) {
            Some(packed) => ((packed >> self.shift) & 0xF) as u8,
            None => 0,
        }
    }

    /// `str.isalnum()`.
    #[inline]
    pub fn is_alnum(self, c: u32) -> bool {
        self.flags(c) & ALNUM != 0
    }

    /// `re`'s `\w`: alphanumeric or `_`.
    #[inline]
    pub fn is_word(self, c: u32) -> bool {
        c == '_' as u32 || self.is_alnum(c)
    }

    /// `str.isspace()`, which is also `re`'s `\s`.
    #[inline]
    pub fn is_space(self, c: u32) -> bool {
        self.flags(c) & SPACE != 0
    }

    /// `str.isdigit()`.
    #[inline]
    pub fn is_digit(self, c: u32) -> bool {
        self.flags(c) & DIGIT != 0
    }

    /// `str.isidentifier()` of the one character.
    #[inline]
    pub fn is_identifier(self, c: u32) -> bool {
        self.flags(c) & IDENTIFIER != 0
    }

    /// The reference's `_is_id_char`.
    #[inline]
    pub fn is_id_char(self, c: u32) -> bool {
        self.is_alnum(c) || c == '_' as u32 || c == '$' as u32 || (c > 127 && self.is_identifier(c))
    }
}

/// The host's rules (`SourcePolicy`), with names as code points.
#[derive(Default)]
pub struct Policy {
    identifiers: HashMap<Vec<u32>, u32>,
    globals: HashMap<Vec<u32>, u32>,
    pub forbid_dynamic_import: bool,
    pub forbid_eval: bool,
    pub forbid_function: bool,
    pub forbid_webassembly: bool,
    pub forbid_computed_global_access: bool,
    pub include_preflight_rules: bool,
    pub ignore_strings_and_comments: bool,
}

impl Policy {
    /// `identifiers` and `globals` in the caller's order: a finding's argument is the index.
    pub fn new(identifiers: Vec<Vec<u32>>, globals: Vec<Vec<u32>>) -> Self {
        let index = |names: Vec<Vec<u32>>| {
            let mut map = HashMap::default();
            for (k, name) in names.into_iter().enumerate() {
                map.entry(name).or_insert(k as u32);
            }
            map
        };
        Policy {
            identifiers: index(identifiers),
            globals: index(globals),
            ..Policy::default()
        }
    }

    fn identifier(&self, name: &[u32]) -> Option<u32> {
        self.identifiers.get(name).copied()
    }

    fn global(&self, name: &[u32]) -> Option<u32> {
        self.globals.get(name).copied()
    }

    fn names(&self) -> impl Iterator<Item = &Vec<u32>> {
        self.identifiers.keys().chain(self.globals.keys())
    }
}

/// `check_source`'s options without a policy (also used with `include_preflight_rules`).
#[derive(Clone, Copy)]
pub struct Options {
    pub allow_import: bool,
    pub allow_dynamic_import: bool,
    pub report_eval: bool,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            allow_import: false,
            allow_dynamic_import: false,
            report_eval: true,
        }
    }
}

/// One finding: a code (above), its argument, and its offset in code points.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Hit {
    pub code: u8,
    pub arg: u32,
    pub offset: usize,
}

/// The rule a code reports: findings are deduplicated per (rule, offset), as in the reference.
fn rule_key(code: u8, arg: u32) -> u32 {
    match code {
        GLOBAL_ALIAS_KEY | GLOBAL_ALIAS_NAME => GLOBAL_ALIAS_NAME as u32,
        PROTO_STRING | PROTO_NAME => PROTO_NAME as u32,
        IMPORT_META | STATIC_IMPORT => STATIC_IMPORT as u32,
        EVAL_CALL | NEW_FUNCTION => EVAL_CALL as u32,
        NAMED => 256 + arg,
        _ => code as u32,
    }
}

/// The findings so far, in the reference's insertion order (deduplicated at the end).
#[derive(Default)]
pub(crate) struct Findings {
    hits: Vec<Hit>,
}

impl Findings {
    fn add(&mut self, code: u8, arg: u32, offset: usize) {
        self.hits.push(Hit { code, arg, offset });
    }
}

/// ASCII `s` equals the code points `t`.
#[inline]
pub(crate) fn eq(t: &[u32], s: &str) -> bool {
    t.len() == s.len() && t.iter().zip(s.bytes()).all(|(&a, b)| a == b as u32)
}

/// Scan `code` (code points) and return the findings, sorted by offset as the reference sorts
/// them by line and column (stable: findings at one offset keep their order).
pub fn scan(code: &[u32], policy: Option<&Policy>, options: Options, cls: Classes) -> Vec<Hit> {
    let mut found = Findings::default();
    let usability = policy.is_none_or(|p| p.include_preflight_rules);
    let precise = policy.is_some_and(|p| p.ignore_strings_and_comments);
    if usability || precise {
        tokens::scan(
            code,
            policy.is_some(),
            usability.then_some(options),
            policy.filter(|_| precise),
            cls,
            &mut found,
        );
    }
    if let Some(policy) = policy.filter(|_| !precise) {
        text::scan(policy, code, cls, &mut found);
    }
    // The reference drops a repeated (rule, offset) when it is added, then sorts stably by
    // position. Sorting stably first and then keeping the first of each rule at each offset
    // gives the same list. Few findings share an offset (one word or token each, at most two
    // readings), so the scan of each offset's group is short.
    let mut hits = found.hits;
    hits.sort_by_key(|h| h.offset);
    let mut out: Vec<Hit> = Vec::with_capacity(hits.len());
    let mut group = 0;
    for hit in hits {
        if out.last().is_none_or(|last| last.offset != hit.offset) {
            group = out.len();
        }
        let key = rule_key(hit.code, hit.arg);
        if !out[group..]
            .iter()
            .any(|seen| rule_key(seen.code, seen.arg) == key)
        {
            out.push(hit);
        }
    }
    out
}

/// 1-based (line, column) of each offset (sorted ascending), counting `\n` only, as the
/// reference's `bisect_right` over line starts does.
pub fn positions(code: &[u32], hits: &[Hit]) -> Vec<(usize, usize)> {
    let mut out = Vec::with_capacity(hits.len());
    let (mut line, mut line_start, mut at) = (1usize, 0usize, 0usize);
    for hit in hits {
        while at < hit.offset.min(code.len()) {
            if code[at] == '\n' as u32 {
                line += 1;
                line_start = at + 1;
            }
            at += 1;
        }
        out.push((line, hit.offset - line_start + 1));
    }
    out
}

fn too_large() -> PyErr {
    PyValueError::new_err("source too large for the native scanner")
}

/// The code points of a Python `str`, lone surrogates included, if it is at most
/// `MAX_SOURCE_BYTES` of UTF-8 (a lone surrogate counts 3 bytes, as `surrogatepass` encodes it).
fn code_points(s: &Bound<'_, PyString>) -> PyResult<Vec<u32>> {
    if s.len()? > MAX_SOURCE_BYTES {
        return Err(too_large()); // never fewer bytes than characters
    }
    if let Ok(text) = s.to_str() {
        if text.len() > MAX_SOURCE_BYTES {
            return Err(too_large());
        }
        return Ok(text.chars().map(|c| c as u32).collect());
    }
    // A lone surrogate: `str.encode` itself (never a subclass's), one code point per 4 bytes.
    let py = s.py();
    let raw = py.get_type::<PyString>().call_method1(
        intern!(py, "encode"),
        (s, intern!(py, "utf-32-le"), intern!(py, "surrogatepass")),
    )?;
    let bytes = raw.cast::<PyBytes>()?.as_bytes();
    let points: Vec<u32> = (0..bytes.len() / 4)
        .map(|i| {
            u32::from_le_bytes([
                bytes[4 * i],
                bytes[4 * i + 1],
                bytes[4 * i + 2],
                bytes[4 * i + 3],
            ])
        })
        .collect();
    let utf8: usize = points
        .iter()
        .map(|&c| match c {
            0..=0x7F => 1,
            0x80..=0x7FF => 2,
            0x800..=0xFFFF => 3,
            _ => 4,
        })
        .sum();
    if utf8 > MAX_SOURCE_BYTES {
        return Err(too_large());
    }
    Ok(points)
}

fn name_points(names: Vec<Bound<'_, PyString>>) -> PyResult<Vec<Vec<u32>>> {
    names.iter().map(code_points).collect()
}

/// `_scan_source`'s policy: (forbidden identifiers, forbidden globals, flags).
type PolicySpec<'py> = (Vec<Bound<'py, PyString>>, Vec<Bound<'py, PyString>>, u32);

// Policy flags as `_scan_source` takes them.
const P_DYNAMIC_IMPORT: u32 = 1;
const P_EVAL: u32 = 2;
const P_FUNCTION: u32 = 4;
const P_WEBASSEMBLY: u32 = 8;
const P_COMPUTED: u32 = 16;
const P_PREFLIGHT_RULES: u32 = 32;
const P_PRECISE: u32 = 64;

/// `check_source`'s scan: a list of `(code, line, column, arg)`, sorted as `check_source` lists
/// its findings. `policy` is `(forbidden_identifiers, forbidden_globals, flags)` or None. The
/// size limit of a policy is the caller's to check first. Raises `ValueError` for a text over
/// `MAX_SOURCE_BYTES` or a Unicode version the tables do not cover. The scan runs with the GIL
/// released, on a copy of the text.
#[pyfunction]
#[pyo3(signature = (code, unicode_version, policy=None, allow_import=false, allow_dynamic_import=false, report_eval=true))]
pub fn _scan_source(
    py: Python<'_>,
    code: &Bound<'_, PyString>,
    unicode_version: &str,
    policy: Option<PolicySpec<'_>>,
    allow_import: bool,
    allow_dynamic_import: bool,
    report_eval: bool,
) -> PyResult<Vec<(u8, usize, usize, u32)>> {
    let cls = Classes::for_version(unicode_version)
        .ok_or_else(|| PyValueError::new_err("Unicode version not covered"))?;
    let policy = match policy {
        None => None,
        Some((identifiers, globals, flags)) => {
            let mut p = Policy::new(name_points(identifiers)?, name_points(globals)?);
            p.forbid_dynamic_import = flags & P_DYNAMIC_IMPORT != 0;
            p.forbid_eval = flags & P_EVAL != 0;
            p.forbid_function = flags & P_FUNCTION != 0;
            p.forbid_webassembly = flags & P_WEBASSEMBLY != 0;
            p.forbid_computed_global_access = flags & P_COMPUTED != 0;
            p.include_preflight_rules = flags & P_PREFLIGHT_RULES != 0;
            p.ignore_strings_and_comments = flags & P_PRECISE != 0;
            Some(p)
        }
    };
    let options = Options {
        allow_import,
        allow_dynamic_import,
        report_eval,
    };
    let code = code_points(code)?;
    Ok(py.detach(move || {
        let hits = scan(&code, policy.as_ref(), options, cls);
        positions(&code, &hits)
            .into_iter()
            .zip(&hits)
            .map(|((line, column), h)| (h.code, line, column, h.arg))
            .collect()
    }))
}

#[cfg(test)]
mod tests;
