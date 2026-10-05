//! The default `SourcePolicy` scan (the reference's `_scan_text`): it fails closed. Every escape
//! is decoded wherever it appears and the whole decoded text is read, comments, strings,
//! templates and regular expressions included. Nothing depends on telling a regex from a division.

use rustc_hash::FxHashSet as HashSet;

use super::{
    eq, Classes, Findings, Policy, FORBIDDEN_COMPUTED_GLOBAL_ACCESS, FORBIDDEN_DYNAMIC_IMPORT,
    FORBIDDEN_EVAL, FORBIDDEN_FUNCTION_CONSTRUCTOR, FORBIDDEN_GLOBAL, FORBIDDEN_IDENTIFIER,
    FORBIDDEN_STRING_TIMER, FORBIDDEN_WEBASSEMBLY,
};

/// How far the `constructor` test looks back (and forward to the parameters, and past them).
const BACK: usize = 64;
/// The longest parameter list the `constructor` test matches.
const PARAMS: usize = 1024;
/// Most findings one reading lists (`MAX_POLICY_FINDINGS`).
pub const MAX_POLICY_FINDINGS: usize = 1000;

const BACKSLASH: u32 = '\\' as u32;
const LS: u32 = 0x2028;
const PS: u32 = 0x2029;

#[inline]
fn is(c: u32, ch: char) -> bool {
    c == ch as u32
}

#[inline]
fn is_hex(c: u32) -> bool {
    matches!(char::from_u32(c), Some('0'..='9' | 'a'..='f' | 'A'..='F'))
}

#[inline]
fn hex_value(digits: &[u32]) -> u32 {
    digits.iter().fold(0u32, |v, &c| {
        let d = char::from_u32(c).and_then(|c| c.to_digit(16)).unwrap_or(0);
        v.saturating_mul(16).saturating_add(d)
    })
}

#[inline]
fn is_terminator(c: u32) -> bool {
    is(c, '\n') || is(c, '\r') || c == LS || c == PS
}

/// The character an identity escape (`\c`) stands for (`_SIMPLE_ESCAPES`).
#[inline]
pub(super) fn simple_escape(c: u32) -> u32 {
    match char::from_u32(c) {
        Some('n') => '\n' as u32,
        Some('r') => '\r' as u32,
        Some('t') => '\t' as u32,
        Some('b') => 0x08,
        Some('f') => 0x0C,
        Some('v') => 0x0B,
        Some('0') => 0,
        _ => c,
    }
}

enum Replacement {
    One(u32),
    Continuation,
}

/// The reference's `_ESCAPE` at the backslash at `i`: (its end, what it decodes to), or None
/// (only for a backslash that ends the text).
fn escape_at(code: &[u32], i: usize) -> Option<(usize, Replacement)> {
    let n = code.len();
    let c1 = *code.get(i + 1)?;
    if is(c1, 'u') {
        // `u\{0*([0-9A-Fa-f]{1,6})\}`
        if code.get(i + 2).is_some_and(|&c| is(c, '{')) {
            let start = i + 3;
            let mut zeros = start;
            while zeros < n && is(code[zeros], '0') {
                zeros += 1;
            }
            let mut end = zeros;
            while end < n && is_hex(code[end]) {
                end += 1;
            }
            if end < n && is(code[end], '}') && end > start && end - zeros <= 6 {
                let value = hex_value(&code[zeros..end]);
                let ch = if value <= 0x10FFFF { value } else { 'u' as u32 };
                return Some((end + 1, Replacement::One(ch)));
            }
        }
        // `u([0-9A-Fa-f]{4})`
        if i + 6 <= n && code[i + 2..i + 6].iter().all(|&c| is_hex(c)) {
            return Some((i + 6, Replacement::One(hex_value(&code[i + 2..i + 6]))));
        }
    } else if is(c1, 'x') {
        if i + 4 <= n && code[i + 2..i + 4].iter().all(|&c| is_hex(c)) {
            return Some((i + 4, Replacement::One(hex_value(&code[i + 2..i + 4]))));
        }
    } else if ('0' as u32..='7' as u32).contains(&c1) {
        // `[0-3][0-7]{0,2}|[4-7][0-7]?`
        let most = if c1 <= '3' as u32 { 3 } else { 2 };
        let mut end = i + 2;
        while end < n && end < i + 1 + most && ('0' as u32..='7' as u32).contains(&code[end]) {
            end += 1;
        }
        let value = code[i + 1..end]
            .iter()
            .fold(0u32, |v, &c| v * 8 + (c - '0' as u32));
        return Some((end, Replacement::One(value)));
    } else if is(c1, '\r') && code.get(i + 2).is_some_and(|&c| is(c, '\n')) {
        return Some((i + 3, Replacement::Continuation));
    } else if is_terminator(c1) {
        return Some((i + 2, Replacement::Continuation));
    }
    Some((i + 2, Replacement::One(simple_escape(c1))))
}

/// `code` with every escape decoded, anchors mapping decoded offsets back, and whether any
/// backslash-line-terminator pair was seen (the reference's `_decoded`).
struct Decoded {
    text: Vec<u32>,
    norm_at: Vec<usize>,
    orig_at: Vec<usize>,
    continued: bool,
}

fn decode(code: &[u32], continuation_is_newline: bool) -> Decoded {
    let mut d = Decoded {
        text: Vec::with_capacity(code.len()),
        norm_at: vec![0],
        orig_at: vec![0],
        continued: false,
    };
    let (mut last, mut i) = (0usize, 0usize);
    while i < code.len() {
        if code[i] != BACKSLASH {
            i += 1;
            continue;
        }
        let Some((end, replacement)) = escape_at(code, i) else {
            break; // a backslash that ends the text stays as it is
        };
        d.text.extend_from_slice(&code[last..i]);
        d.norm_at.push(d.text.len());
        d.orig_at.push(i);
        match replacement {
            Replacement::One(c) => d.text.push(c),
            Replacement::Continuation => {
                d.continued = true;
                if continuation_is_newline {
                    d.text.push('\n' as u32);
                }
            }
        }
        d.norm_at.push(d.text.len());
        d.orig_at.push(end);
        last = end;
        i = end;
    }
    d.text.extend_from_slice(&code[last..]);
    d
}

/// What precedes a `constructor` (the reference's `_before`).
#[derive(PartialEq)]
enum Before {
    Start,
    Unknown,
    Char(u32),
}

/// The character before `start`, skipping whitespace and up to two whole `//` comment lines,
/// within bounded windows.
fn before(code: &[u32], start: usize, cls: Classes) -> Before {
    let mut i = start as isize - 1;
    for _hop in 0..3 {
        let floor = (-1isize).max(i - BACK as isize);
        while i > floor && cls.is_space(code[i as usize]) {
            i -= 1;
        }
        if i < 0 {
            return Before::Start;
        }
        if i == floor {
            return Before::Unknown;
        }
        let window = 0isize.max(i - BACK as isize);
        let mut line_start = -1isize;
        let mut p = i;
        while p >= window {
            if is_terminator(code[p as usize]) {
                line_start = p;
                break;
            }
            p -= 1;
        }
        if line_start < 0 || !comment_line(code, line_start as usize + 1, i as usize, cls) {
            return Before::Char(code[i as usize]);
        }
        i = line_start;
    }
    Before::Unknown
}

/// `code[from..=to].lstrip().startswith("//")`.
fn comment_line(code: &[u32], from: usize, to: usize, cls: Classes) -> bool {
    let mut q = from;
    while q <= to && cls.is_space(code[q]) {
        q += 1;
    }
    q < to && is(code[q], '/') && is(code[q + 1], '/')
}

/// From the `(` at `i`: the index after its matching `)`, or None when that cannot be shown
/// cheaply (the reference's `_parameters_end`).
fn parameters_end(code: &[u32], i: usize) -> Option<usize> {
    let mut depth = 0isize;
    let limit = code.len().min(i + PARAMS);
    let mut j = i;
    while j < limit {
        let c = code[j];
        if is(c, '(') || is(c, '[') || is(c, '{') {
            depth += 1;
        } else if is(c, ')') || is(c, ']') || is(c, '}') {
            depth -= 1;
            if depth == 0 {
                return is(c, ')').then_some(j + 1);
            }
            if depth < 0 {
                return None;
            }
        } else if is(c, '\'') || is(c, '"') {
            j += 1;
            while j < limit && code[j] != c {
                if is_terminator(code[j]) {
                    return None;
                }
                j += if code[j] == BACKSLASH { 2 } else { 1 };
            }
            if j >= limit {
                return None;
            }
        } else if is(c, '/') || is(c, '`') {
            return None;
        }
        j += 1;
    }
    None
}

/// Is the `constructor` at `code[start..end]` provably a method definition, `constructor(...) {`?
/// (the reference's `_method_definition`).
fn method_definition(code: &[u32], start: usize, end: usize, cls: Classes) -> bool {
    match before(code, start, cls) {
        Before::Char(c) if is(c, '{') || is(c, '}') || is(c, ';') => {}
        _ => return false,
    }
    let n = code.len();
    let mut j = end;
    let limit = n.min(end + BACK);
    while j < limit && cls.is_space(code[j]) {
        j += 1;
    }
    if j >= limit || !is(code[j], '(') {
        return false;
    }
    let Some(mut close) = parameters_end(code, j) else {
        return false;
    };
    let limit = n.min(close + BACK);
    while close < limit
        && matches!(
            char::from_u32(code[close]),
            Some(' ' | '\t' | '\x0B' | '\x0C' | '\u{A0}' | '\u{FEFF}')
        )
    {
        close += 1;
    }
    close < n && is(code[close], '{')
}

pub(super) fn scan(policy: &Policy, code: &[u32], cls: Classes, found: &mut Findings) {
    let interest = Interest::new(policy);
    let decoded = decode(code, false);
    scan_view(policy, &interest, code, &decoded, cls, found);
    if decoded.continued {
        // A backslash at the end of a comment line is text, and the comment ends at the break:
        // read that way too, or `//x\` + newline + `name` would read as the one word `xname`.
        drop(decoded);
        let decoded = decode(code, true);
        scan_view(policy, &interest, code, &decoded, cls, found);
    }
}

/// The words a scan looks at (the reference's `interest`).
struct Interest {
    words: HashSet<Vec<u32>>,
    longest: usize,
}

const GLOBAL_VALUES: [&str; 5] = ["globalThis", "self", "window", "global", "this"];

impl Interest {
    fn new(policy: &Policy) -> Self {
        let mut words: HashSet<Vec<u32>> = policy.names().cloned().collect();
        let mut add = |w: &str| {
            words.insert(w.chars().map(|c| c as u32).collect());
        };
        if policy.forbid_eval {
            add("eval");
            add("setTimeout");
            add("setInterval");
        }
        if policy.forbid_function {
            add("Function");
            add("constructor");
        }
        if policy.forbid_webassembly {
            add("WebAssembly");
        }
        if policy.forbid_dynamic_import {
            add("import");
        }
        if policy.forbid_computed_global_access {
            GLOBAL_VALUES.iter().for_each(|w| add(w));
            add("Reflect");
            add("constructor");
            add("Function");
        }
        let longest = words.iter().map(Vec::len).max().unwrap_or(0);
        Interest { words, longest }
    }

    fn contains(&self, word: &[u32]) -> bool {
        word.len() <= self.longest && self.words.contains(word)
    }
}

fn skip_space(text: &[u32], mut i: usize, cls: Classes) -> usize {
    while i < text.len() && cls.is_space(text[i]) {
        i += 1;
    }
    i
}

/// `\s*\(\s*['"`]` at `i`.
fn string_argument(text: &[u32], i: usize, cls: Classes) -> bool {
    let i = skip_space(text, i, cls);
    if !text.get(i).is_some_and(|&c| is(c, '(')) {
        return false;
    }
    let i = skip_space(text, i + 1, cls);
    text.get(i)
        .is_some_and(|&c| is(c, '\'') || is(c, '"') || is(c, '`'))
}

/// `\s*\??\.\s*[\w$]` at `i`.
fn dotted(text: &[u32], i: usize, cls: Classes) -> bool {
    let mut i = skip_space(text, i, cls);
    if text.get(i).is_some_and(|&c| is(c, '?')) {
        i += 1;
    }
    if !text.get(i).is_some_and(|&c| is(c, '.')) {
        return false;
    }
    let i = skip_space(text, i + 1, cls);
    text.get(i).is_some_and(|&c| cls.is_word(c) || is(c, '$'))
}

/// `\s*\[` at `i`.
fn bracket(text: &[u32], i: usize, cls: Classes) -> bool {
    let i = skip_space(text, i, cls);
    text.get(i).is_some_and(|&c| is(c, '['))
}

/// `\s*(.?)` at `i`, then whether that character can start a static import
/// (`[\w${*"'.]`): None at the end of the text.
fn static_import_follows(text: &[u32], i: usize, cls: Classes) -> Option<bool> {
    let i = skip_space(text, i, cls);
    text.get(i).map(|&c| {
        cls.is_word(c) || matches!(char::from_u32(c), Some('$' | '{' | '*' | '"' | '\'' | '.'))
    })
}

fn scan_view(
    policy: &Policy,
    interest: &Interest,
    code: &[u32],
    d: &Decoded,
    cls: Classes,
    found: &mut Findings,
) {
    let text = &d.text;
    let original = |at: usize| -> usize {
        let k = d.norm_at.partition_point(|&x| x <= at).saturating_sub(1);
        d.orig_at[k] + at - d.norm_at[k]
    };
    let mut reported = 0usize;
    let mut report = |code: u8, arg: u32, at: usize, reported: &mut usize| {
        *reported += 1;
        found.add(code, arg, original(at));
    };
    let n = text.len();
    let mut i = 0usize;
    while i < n {
        let c = text[i];
        if !(cls.is_word(c) || is(c, '$')) {
            i += 1;
            continue;
        }
        let start = i;
        while i < n && (cls.is_word(text[i]) || is(text[i], '$')) {
            i += 1;
        }
        let end = i;
        let word = &text[start..end];
        if !interest.contains(word) {
            continue;
        }
        if reported >= MAX_POLICY_FINDINGS {
            return; // denied many times over; listing more only costs time
        }
        let after_dot = start > 0
            && is(text[start - 1], '.')
            && !(start >= 3 && text[start - 3..start].iter().all(|&c| is(c, '.')));
        if let Some(k) = policy.identifier(word) {
            report(FORBIDDEN_IDENTIFIER, k, start, &mut reported);
        }
        if let Some(k) = policy.global(word) {
            report(FORBIDDEN_GLOBAL, k, start, &mut reported);
        }
        if policy.forbid_eval {
            if eq(word, "eval") {
                report(FORBIDDEN_EVAL, 0, start, &mut reported);
            } else if (eq(word, "setTimeout") || eq(word, "setInterval"))
                && string_argument(text, end, cls)
            {
                let arg = if eq(word, "setTimeout") { 0 } else { 1 };
                report(FORBIDDEN_STRING_TIMER, arg, start, &mut reported);
            }
        }
        if policy.forbid_function
            && (eq(word, "Function")
                || (eq(word, "constructor")
                    && !method_definition(code, original(start), original(end), cls)))
        {
            report(FORBIDDEN_FUNCTION_CONSTRUCTOR, 0, start, &mut reported);
        }
        if policy.forbid_webassembly && eq(word, "WebAssembly") {
            report(FORBIDDEN_WEBASSEMBLY, 0, start, &mut reported);
        }
        if policy.forbid_dynamic_import
            && eq(word, "import")
            && !after_dot
            && static_import_follows(text, end, cls) == Some(false)
        {
            report(FORBIDDEN_DYNAMIC_IMPORT, 0, start, &mut reported);
        }
        if policy.forbid_computed_global_access
            && ((GLOBAL_VALUES.iter().any(|g| eq(word, g))
                && !after_dot
                && !dotted(text, end, cls))
                || eq(word, "Reflect")
                || ((eq(word, "constructor") || eq(word, "Function")) && bracket(text, end, cls)))
        {
            report(FORBIDDEN_COMPUTED_GLOBAL_ACCESS, 0, start, &mut reported);
        }
    }
}
