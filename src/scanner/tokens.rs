//! The tokenizer (the reference's `_tokenize`) and the rules that read its tokens: the usability
//! rules of `check_source` and the precise `SourcePolicy` mode (`ignore_strings_and_comments`).
//!
//! Tokens stream through a short window: the rules for token `k` read tokens `k - 3 ..= k + 2`
//! and the tokenizer reads the last two, so memory does not grow with the number of tokens.

use std::collections::VecDeque;

use super::text::simple_escape;
use super::{
    eq, Classes, Findings, Options, Policy, CONSTRUCTOR_CONSTRUCTOR, DYNAMIC_IMPORT, EVAL_CALL,
    FORBIDDEN_COMPUTED_GLOBAL_ACCESS, FORBIDDEN_DYNAMIC_IMPORT, FORBIDDEN_EVAL,
    FORBIDDEN_FUNCTION_CONSTRUCTOR, FORBIDDEN_GLOBAL, FORBIDDEN_IDENTIFIER, FORBIDDEN_STRING_TIMER,
    FORBIDDEN_WEBASSEMBLY, GLOBALS, GLOBAL_ALIAS_KEY, GLOBAL_ALIAS_NAME, IMPORT_META, NAMED, NAMES,
    NAME_NEEDS, NEW_FUNCTION, PROTO_NAME, PROTO_STRING, STATIC_EXPORT, STATIC_IMPORT,
};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Id,
    Num,
    Str,
    Tpl,
    Re,
    Punct,
}

#[derive(Debug)]
enum Text {
    /// `src[start..end]`
    Src(usize, usize),
    /// An identifier with decoded escapes.
    Own(Vec<u32>),
}

#[derive(Debug)]
struct Tok {
    kind: Kind,
    off: usize,
    text: Text,
    /// A `}` that closed an object literal (`closes_expression` in the reference).
    closes_expression: bool,
    /// A `)` that closed a control head such as `if (...)` (`closes_control`).
    closes_control: bool,
}

const REGEX_AFTER_WORD: [&str; 14] = [
    "return",
    "typeof",
    "instanceof",
    "in",
    "of",
    "new",
    "delete",
    "void",
    "throw",
    "case",
    "do",
    "else",
    "yield",
    "await",
];
const POLICY_REGEX_AFTER_WORD: [&str; 11] = [
    "return",
    "typeof",
    "instanceof",
    "in",
    "new",
    "delete",
    "void",
    "throw",
    "case",
    "do",
    "else",
];
const CONTROL_HEADS: [&str; 4] = ["if", "while", "for", "with"];
const MAX_BRACE_DIGITS: usize = 8;

#[inline]
fn is(c: u32, ch: char) -> bool {
    c == ch as u32
}

#[inline]
fn one_of(t: &[u32], words: &[&str]) -> bool {
    words.iter().any(|w| eq(t, w))
}

#[inline]
fn is_hex(c: u32) -> bool {
    matches!(char::from_u32(c), Some('0'..='9' | 'a'..='f' | 'A'..='F'))
}

fn hex_value(digits: &[u32]) -> u32 {
    digits.iter().fold(0u32, |v, &c| {
        let d = char::from_u32(c).and_then(|c| c.to_digit(16)).unwrap_or(0);
        v.saturating_mul(16).saturating_add(d)
    })
}

/// The reference's `_unicode_escape`: a backslash-u escape starting at `j` (the character and
/// the index after it), or None.
fn unicode_escape(src: &[u32], j: usize) -> Option<(u32, usize)> {
    let n = src.len();
    if !(j + 1 < n && is(src[j], '\\') && is(src[j + 1], 'u')) {
        return None;
    }
    if j + 2 < n && is(src[j + 2], '{') {
        // Bounded: an unclosed `\u{` must not make every escape scan to the end of the text.
        let from = j + 3;
        let to = n.min(j + 3 + MAX_BRACE_DIGITS + 1);
        let end = (from..to).find(|&k| is(src[k], '}'))?;
        let digits = &src[from..end];
        if !digits.is_empty() && digits.len() <= 8 && digits.iter().all(|&c| is_hex(c)) {
            let value = hex_value(digits);
            if value <= 0x10FFFF {
                return Some((value, end + 1));
            }
        }
        return None;
    }
    if j + 6 <= n && src[j + 2..j + 6].iter().all(|&c| is_hex(c)) {
        return Some((hex_value(&src[j + 2..j + 6]), j + 6));
    }
    None
}

#[inline]
fn is_line_end(c: u32) -> bool {
    is(c, '\n') || is(c, '\r') || c == 0x2028 || c == 0x2029
}

/// A string literal's (or a template's) text as the engine reads it, best effort (`_cooked`).
fn cooked(raw: &[u32]) -> Vec<u32> {
    let n = raw.len();
    let mut out = Vec::with_capacity(n);
    let mut i = 0;
    while i < n {
        let ch = raw[i];
        if !is(ch, '\\') || i + 1 >= n {
            out.push(ch);
            i += 1;
            continue;
        }
        let nxt = raw[i + 1];
        if is(nxt, 'u') {
            if let Some((c, end)) = unicode_escape(raw, i) {
                out.push(c);
                i = end;
                continue;
            }
        } else if is(nxt, 'x') && i + 4 <= n && raw[i + 2..i + 4].iter().all(|&c| is_hex(c)) {
            out.push(hex_value(&raw[i + 2..i + 4]));
            i += 4;
            continue;
        } else if is_line_end(nxt) {
            // a line continuation
            i += if is(nxt, '\r') && i + 2 < n && is(raw[i + 2], '\n') {
                3
            } else {
                2
            };
            continue;
        }
        out.push(simple_escape(nxt));
        i += 2;
    }
    out
}

/// The findings of one token, before they are added (at most 6 policy rules, or 2 usability
/// ones, apply to a token).
#[derive(Default)]
struct Pending {
    items: [(u8, u32); 8],
    len: usize,
}

impl Pending {
    fn push(&mut self, item: (u8, u32)) {
        if let Some(slot) = self.items.get_mut(self.len) {
            *slot = item;
            self.len += 1;
        }
    }

    fn items(&self) -> &[(u8, u32)] {
        &self.items[..self.len]
    }
}

/// The token stream: a window over the tokens with absolute indexes.
struct Window {
    toks: VecDeque<Tok>,
    /// The absolute index of `toks[0]`.
    base: usize,
}

impl Window {
    fn len(&self) -> usize {
        self.base + self.toks.len()
    }

    fn at(&self, k: isize) -> Option<&Tok> {
        if k < self.base as isize {
            return None;
        }
        self.toks.get(k as usize - self.base)
    }

    fn last(&self, back: usize) -> Option<&Tok> {
        self.at(self.len() as isize - 1 - back as isize)
    }
}

struct Scanner<'a> {
    src: &'a [u32],
    cls: Classes,
    policy_tokens: bool,
    usability: Option<Options>,
    policy: Option<&'a Policy>,
    window: Window,
    /// The next token whose rules run.
    next_rule: usize,
    found: &'a mut Findings,
}

/// Tokenize `src` and apply the usability rules (`usability`) and the precise policy rules
/// (`policy`) to every token. `policy_tokens` is the reference's `policy=True` tokenization.
pub(super) fn scan(
    src: &[u32],
    policy_tokens: bool,
    usability: Option<Options>,
    policy: Option<&Policy>,
    cls: Classes,
    found: &mut Findings,
) {
    let mut s = Scanner {
        src,
        cls,
        policy_tokens,
        usability,
        policy,
        window: Window {
            toks: VecDeque::new(),
            base: 0,
        },
        next_rule: 0,
        found,
    };
    s.tokenize();
    while s.next_rule < s.window.len() {
        s.rules(s.next_rule);
        s.next_rule += 1;
    }
}

impl Scanner<'_> {
    fn text<'t>(&'t self, t: &'t Tok) -> &'t [u32] {
        match &t.text {
            Text::Src(a, b) => &self.src[*a..*b],
            Text::Own(v) => v,
        }
    }

    fn push(&mut self, kind: Kind, off: usize, text: Text) {
        self.window.toks.push_back(Tok {
            kind,
            off,
            text,
            closes_expression: false,
            closes_control: false,
        });
        self.flush();
    }

    /// Run the rules of every token whose two successors exist, then drop what no rule and no
    /// tokenizer step reads again.
    fn flush(&mut self) {
        while self.next_rule + 2 < self.window.len() {
            self.rules(self.next_rule);
            self.next_rule += 1;
        }
        let keep_from = self.next_rule.saturating_sub(3);
        while self.window.base < keep_from && self.window.toks.len() > 2 {
            self.window.toks.pop_front();
            self.window.base += 1;
        }
    }

    fn punct(&mut self, off: usize, len: usize) {
        self.push(Kind::Punct, off, Text::Src(off, off + len));
    }

    /// `after_access(k)`: the token before `k` is `.` or `?.`.
    fn after_access(&self, k: isize) -> bool {
        k > 0
            && self
                .window
                .at(k - 1)
                .is_some_and(|t| t.kind == Kind::Punct && self.is_access(t))
    }

    fn is_access(&self, t: &Tok) -> bool {
        let text = self.text(t);
        eq(text, ".") || eq(text, "?.")
    }

    fn regex_allowed(&self) -> bool {
        let Some(last) = self.window.last(0) else {
            return true;
        };
        let text = self.text(last);
        match last.kind {
            Kind::Num | Kind::Str | Kind::Tpl | Kind::Re => return false,
            Kind::Id => {
                if !self.policy_tokens {
                    return one_of(text, &REGEX_AFTER_WORD);
                }
                // `obj.in / x / 2` divides; `of`, `yield` and `await` can be plain variables.
                return one_of(text, &POLICY_REGEX_AFTER_WORD)
                    && !self.after_access(self.window.len() as isize - 1);
            }
            Kind::Punct => {}
        }
        if self.policy_tokens {
            if (eq(text, "+") || eq(text, "-"))
                && self.window.len() > 1
                && self
                    .window
                    .last(1)
                    .is_some_and(|p| self.text(p) == text && p.off + 1 == last.off)
            {
                return false; // `a++ / x / 2`: a postfix operator, then a division
            }
            if eq(text, "}") {
                return !last.closes_expression; // `{} / x / 2` divides
            }
            if eq(text, ")") {
                return last.closes_control; // `if (a) /re/.test(s)` starts a regex
            }
        }
        !(eq(text, ")") || eq(text, "]"))
    }

    /// Policy only: would a `{` here open an object literal (rather than a block)?
    fn expression_expected(&self) -> bool {
        let Some(last) = self.window.last(0) else {
            return false;
        };
        let text = self.text(last);
        match last.kind {
            Kind::Id => {
                return one_of(text, &POLICY_REGEX_AFTER_WORD)
                    && !self.after_access(self.window.len() as isize - 1);
            }
            Kind::Punct => {}
            _ => return false,
        }
        if eq(text, ">")
            && self.window.len() > 1
            && self
                .window
                .last(1)
                .is_some_and(|p| eq(self.text(p), "=") && p.off + 1 == last.off)
        {
            return false; // `=> {`: an arrow function's body
        }
        text.len() == 1 && "([,=:?!~+-*%&|^<>/".chars().any(|c| text[0] == c as u32)
    }

    /// From just after a backtick or a closing `}`: (index, hit the closing backtick).
    fn scan_template(&self, mut j: usize) -> (usize, bool) {
        let src = self.src;
        let n = src.len();
        while j < n {
            let ch = src[j];
            if is(ch, '\\') {
                j += 2;
            } else if is(ch, '`') {
                return (j + 1, true);
            } else if is(ch, '$') && j + 1 < n && is(src[j + 1], '{') {
                return (j + 2, false);
            } else {
                j += 1;
            }
        }
        (n, true) // unterminated: stop quietly
    }

    fn starts_with(&self, i: usize, s: &str) -> bool {
        let src = self.src;
        s.chars()
            .enumerate()
            .all(|(k, c)| src.get(i + k).is_some_and(|&x| x == c as u32))
    }

    fn tokenize(&mut self) {
        let src = self.src;
        let cls = self.cls;
        let policy = self.policy_tokens;
        let n = src.len();
        let mut i = 0usize;
        // b'b' for `{`, b'o' for an object literal's `{` (policy only), b't' for `${`
        let mut stack: Vec<u8> = Vec::new();
        // Policy only: whether each open `(` heads a control statement (`if (...)`).
        let mut parens: Vec<bool> = Vec::new();
        let mut no_regex_before = 0usize;
        while i < n {
            let ch = src[i];
            let c = char::from_u32(ch);
            if matches!(
                c,
                Some(
                    ' ' | '\t'
                        | '\r'
                        | '\n'
                        | '\x0C'
                        | '\x0B'
                        | '\u{A0}'
                        | '\u{FEFF}'
                        | '\u{2028}'
                        | '\u{2029}'
                )
            ) || (policy && cls.is_space(ch))
            {
                i += 1;
            } else if self.starts_with(i, "//") {
                let mut j = i;
                while j < n && !is_line_end(src[j]) {
                    j += 1;
                }
                i = j;
            } else if self.starts_with(i, "/*") {
                let mut j = i + 2;
                while j + 1 < n && !(is(src[j], '*') && is(src[j + 1], '/')) {
                    j += 1;
                }
                i = if j + 1 < n { j + 2 } else { n };
            } else if is(ch, '\'') || is(ch, '"') {
                let mut j = i + 1;
                while j < n && src[j] != ch && !is(src[j], '\n') && !is(src[j], '\r') {
                    j += if is(src[j], '\\') { 2 } else { 1 };
                }
                self.push(Kind::Str, i, Text::Src(i + 1, j.min(n)));
                i = (j + 1).min(n);
            } else if is(ch, '`') {
                let (j, closed) = self.scan_template(i + 1);
                let text = if policy && closed {
                    Text::Src(i + 1, (j - 1).max(i + 1))
                } else {
                    Text::Src(i, i)
                };
                self.push(Kind::Tpl, i, text);
                if !closed {
                    stack.push(b't');
                }
                i = j;
            } else if is(ch, '{') {
                stack.push(if policy && self.expression_expected() {
                    b'o'
                } else {
                    b'b'
                });
                self.punct(i, 1);
                i += 1;
            } else if is(ch, '}') {
                if stack.last() == Some(&b't') {
                    stack.pop();
                    let (j, closed) = self.scan_template(i + 1);
                    if !closed {
                        stack.push(b't');
                    }
                    i = j;
                } else {
                    let closes_expression = stack.pop() == Some(b'o');
                    self.punct(i, 1);
                    if let Some(t) = self.window.toks.back_mut() {
                        t.closes_expression = closes_expression;
                    }
                    i += 1;
                }
            } else if policy && (is(ch, '(') || is(ch, ')')) {
                let mut closes_control = false;
                if is(ch, '(') {
                    let head = self.window.len() as isize - 1;
                    let control = self.window.at(head).is_some_and(|t| {
                        t.kind == Kind::Id && one_of(self.text(t), &CONTROL_HEADS)
                    }) && !self.after_access(head);
                    parens.push(control);
                } else if parens.pop() == Some(true) {
                    closes_control = true;
                }
                self.punct(i, 1);
                if let Some(t) = self.window.toks.back_mut() {
                    t.closes_control = closes_control;
                }
                i += 1;
            } else if is(ch, '/') && i >= no_regex_before && self.regex_allowed() {
                let (mut j, mut in_class) = (i + 1, false);
                while j < n && !is(src[j], '\n') && !is(src[j], '\r') {
                    let c = src[j];
                    if is(c, '\\') {
                        j += 2;
                        continue;
                    }
                    if is(c, '[') {
                        in_class = true;
                    } else if is(c, ']') {
                        in_class = false;
                    } else if is(c, '/') && !in_class {
                        break;
                    }
                    j += 1;
                }
                if j < n && is(src[j], '/') {
                    j += 1;
                    while j < n && cls.is_id_char(src[j]) {
                        j += 1; // flags
                    }
                    self.push(Kind::Re, i, Text::Src(i, i));
                    i = j;
                } else {
                    // Not a regex after all (ran into a newline): a division sign. No `/` before
                    // that line end can start one either, which keeps `/[/[/[...` linear.
                    no_regex_before = j;
                    self.punct(i, 1);
                    i += 1;
                }
            } else if !policy && cls.is_id_char(ch) && !cls.is_digit(ch) {
                let mut j = i + 1;
                while j < n && cls.is_id_char(src[j]) {
                    j += 1;
                }
                self.push(Kind::Id, i, Text::Src(i, j));
                i = j;
            } else if policy
                && ((cls.is_id_char(ch) && !cls.is_digit(ch)) || self.starts_with(i, "\\u"))
            {
                let mut j = i;
                let mut own: Option<Vec<u32>> = None;
                while j < n {
                    if cls.is_id_char(src[j]) {
                        if let Some(v) = own.as_mut() {
                            v.push(src[j]);
                        }
                        j += 1;
                        continue;
                    }
                    let Some((decoded, end)) = unicode_escape(src, j) else {
                        break;
                    };
                    own.get_or_insert_with(|| src[i..j].to_vec()).push(decoded);
                    j = end;
                }
                if j == i {
                    // a backslash that starts no valid escape
                    self.punct(i, 1);
                    i += 1;
                } else {
                    let text = match own {
                        Some(v) => Text::Own(v),
                        None => Text::Src(i, j),
                    };
                    self.push(Kind::Id, i, text);
                    i = j;
                }
            } else if cls.is_digit(ch) || (is(ch, '.') && i + 1 < n && cls.is_digit(src[i + 1])) {
                let mut j = i + 1;
                while j < n && (cls.is_id_char(src[j]) || is(src[j], '.')) {
                    j += 1;
                }
                self.push(Kind::Num, i, Text::Src(i, j));
                i = j;
            } else if self.starts_with(i, "?.") && !(i + 2 < n && cls.is_digit(src[i + 2])) {
                self.punct(i, 2);
                i += 2;
            } else {
                self.punct(i, 1);
                i += 1;
            }
        }
    }

    fn rules(&mut self, k: usize) {
        if let Some(options) = self.usability {
            self.usability_rules(k as isize, options);
        }
        if let Some(policy) = self.policy {
            self.policy_rules(k as isize, policy);
        }
    }

    fn text_at(&self, k: isize) -> Option<&[u32]> {
        self.window.at(k).map(|t| self.text(t))
    }

    fn text_is(&self, k: isize, s: &str) -> bool {
        self.text_at(k).is_some_and(|t| eq(t, s))
    }

    /// The index in `GLOBALS` of token `k`, an identifier.
    fn global_at(&self, k: isize) -> Option<usize> {
        let t = self.window.at(k)?;
        if t.kind != Kind::Id {
            return None;
        }
        let text = self.text(t);
        GLOBALS.iter().position(|g| eq(text, g))
    }

    /// The usability rules of `check_source` for token `k`.
    fn usability_rules(&mut self, k: isize, options: Options) {
        let Some(tok) = self.window.at(k) else {
            return;
        };
        let (kind, off) = (tok.kind, tok.off);
        let text = self.text(tok);
        let after_dot = self
            .window
            .at(k - 1)
            .is_some_and(|p| p.kind == Kind::Punct && self.is_access(p));
        let via_global = after_dot && self.global_at(k - 2).is_some();
        let mut hits = Pending::default();

        if kind == Kind::Str {
            // `globalThis["fetch"]`: only a plain literal is seen.
            if self.text_is(k - 1, "[") {
                if let (Some(g), Some(name)) = (
                    self.global_at(k - 2),
                    NAMES.iter().position(|w| eq(text, w)),
                ) {
                    hits.push((GLOBAL_ALIAS_KEY, (16 * g + name) as u32));
                }
            }
            if eq(text, "__proto__") {
                hits.push((PROTO_STRING, 0));
            }
        } else if kind == Kind::Id {
            if after_dot && !via_global {
                if eq(text, "constructor")
                    && self
                        .window
                        .at(k - 2)
                        .is_some_and(|p| p.kind == Kind::Id && eq(self.text(p), "constructor"))
                {
                    hits.push((CONSTRUCTOR_CONSTRUCTOR, 0));
                }
                if eq(text, "__proto__") {
                    hits.push((PROTO_NAME, 0));
                }
            } else if eq(text, "__proto__") {
                hits.push((PROTO_NAME, 0));
            } else if eq(text, "export") {
                if !options.allow_import {
                    hits.push((STATIC_EXPORT, 0));
                }
            } else if eq(text, "import") {
                if self.text_is(k + 1, "(") {
                    if !options.allow_dynamic_import {
                        hits.push((DYNAMIC_IMPORT, 0));
                    }
                } else if self.text_is(k + 1, ".") {
                    if !options.allow_import {
                        hits.push((IMPORT_META, 0));
                    }
                } else if !options.allow_import {
                    hits.push((STATIC_IMPORT, 0));
                }
            } else if let Some(name) = NAMES.iter().position(|w| eq(text, w)) {
                let follows = match NAME_NEEDS[name] {
                    None => true,
                    Some(needs) => self
                        .text_at(k + 1)
                        .is_some_and(|t| (t.len() == 1 && t[0] == needs as u32) || eq(t, "?.")),
                };
                if via_global {
                    hits.push((GLOBAL_ALIAS_NAME, name as u32));
                } else if follows {
                    hits.push((NAMED, name as u32));
                }
            } else if eq(text, "eval") && options.report_eval {
                if self.text_is(k + 1, "(") {
                    hits.push((EVAL_CALL, 0));
                }
            } else if eq(text, "Function") && options.report_eval {
                let after_new = self
                    .window
                    .at(k - 1)
                    .is_some_and(|p| p.kind == Kind::Id && eq(self.text(p), "new"));
                if after_new && self.text_is(k + 1, "(") {
                    hits.push((NEW_FUNCTION, 0));
                }
            }
        }
        for &(code, arg) in hits.items() {
            self.found.add(code, arg, off);
        }
    }

    /// `_computed_global`: token `k` is a `[`; is it a computed key on a global object or the
    /// Function constructor?
    fn computed_global(&self, k: isize) -> bool {
        let mut base_at = k - 1;
        if self
            .window
            .at(base_at)
            .is_some_and(|b| b.kind == Kind::Punct && eq(self.text(b), "?."))
        {
            base_at -= 1;
        }
        let Some(base) = self.window.at(base_at) else {
            return false;
        };
        if base.kind != Kind::Id {
            return false;
        }
        let name = self.text(base);
        if GLOBALS.iter().any(|g| eq(name, g)) || eq(name, "this") {
            if self
                .window
                .at(base_at - 1)
                .is_some_and(|p| p.kind == Kind::Punct && self.is_access(p))
            {
                return false; // `x.self[k]`: a property that happens to be called `self`
            }
        } else if !(eq(name, "constructor") || eq(name, "Function")) {
            return false;
        }
        let literal = self
            .window
            .at(k + 1)
            .is_some_and(|key| matches!(key.kind, Kind::Str | Kind::Tpl | Kind::Num))
            && self
                .window
                .at(k + 2)
                .is_some_and(|c| c.kind == Kind::Punct && eq(self.text(c), "]"));
        !literal
    }

    /// `_apply_policy` for token `k`.
    fn policy_rules(&mut self, k: isize, policy: &Policy) {
        let Some(tok) = self.window.at(k) else {
            return;
        };
        let (kind, off) = (tok.kind, tok.off);
        let after_dot = self
            .window
            .at(k - 1)
            .is_some_and(|p| p.kind == Kind::Punct && self.is_access(p));
        let mut hits = Pending::default();
        if kind == Kind::Punct && eq(self.text(tok), "[") {
            if policy.forbid_computed_global_access && self.computed_global(k) {
                self.found.add(FORBIDDEN_COMPUTED_GLOBAL_ACCESS, 0, off);
            }
            return;
        }
        let cooked_name;
        let (name, on_global, called): (&[u32], bool, bool) = match kind {
            Kind::Str | Kind::Tpl => {
                // A computed key, `x["name"]`, read as the engine reads the literal.
                if !(self.text_is(k - 1, "[") && self.text_is(k + 1, "]")) {
                    return;
                }
                cooked_name = cooked(self.text(tok));
                (
                    &cooked_name,
                    self.global_at(k - 2).is_some(),
                    self.text_is(k + 2, "("),
                )
            }
            Kind::Id => (
                self.text(tok),
                !after_dot || self.global_at(k - 2).is_some(),
                self.text_is(k + 1, "("),
            ),
            _ => return,
        };

        if let Some(index) = policy.identifier(name) {
            hits.push((FORBIDDEN_IDENTIFIER, index));
        }
        if on_global {
            if let Some(index) = policy.global(name) {
                hits.push((FORBIDDEN_GLOBAL, index));
            }
        }
        if policy.forbid_eval {
            if eq(name, "eval") {
                hits.push((FORBIDDEN_EVAL, 0));
            } else if (eq(name, "setTimeout") || eq(name, "setInterval"))
                && on_global
                && called
                && kind == Kind::Id
            {
                // `setTimeout("code", ...)`: the first argument is a string or a template.
                if self
                    .window
                    .at(k + 2)
                    .is_some_and(|f| matches!(f.kind, Kind::Str | Kind::Tpl))
                {
                    let arg = if eq(name, "setTimeout") { 0 } else { 1 };
                    hits.push((FORBIDDEN_STRING_TIMER, arg));
                }
            }
        }
        if policy.forbid_function
            && (eq(name, "Function")
                || (eq(name, "constructor") && called && (after_dot || kind != Kind::Id)))
        {
            hits.push((FORBIDDEN_FUNCTION_CONSTRUCTOR, 0));
        }
        if policy.forbid_webassembly && eq(name, "WebAssembly") {
            hits.push((FORBIDDEN_WEBASSEMBLY, 0));
        }
        if policy.forbid_dynamic_import
            && kind == Kind::Id
            && eq(name, "import")
            && !after_dot
            && called
        {
            hits.push((FORBIDDEN_DYNAMIC_IMPORT, 0));
        }
        for &(code, arg) in hits.items() {
            self.found.add(code, arg, off);
        }
    }
}
