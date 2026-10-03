//! JSON for the isolation wire, in Rust: parse a frame and decode its values in one pass, and write
//! a message with its values encoded in one pass.
//!
//! `wire.rs` ports the value codec; this removes the two `json` passes around it. The wire format
//! is unchanged (plain JSON), so a peer using the Python codec interoperates, and
//! `tests/test_wire_native.py` runs both against each other.
//!
//! The parser is hand-written, not `serde_json`, for one reason: JavaScript strings may hold lone
//! surrogates, Python's `json` round-trips them as `\ud800` escapes, and `serde_json` refuses
//! them. A parser that rejected data the Python path accepts would turn valid guest values into a
//! "protocol violation" that kills the worker.
//!
//! It parses into a compact Rust tree first (no Python allocation, GIL released), which also lets
//! it refuse an oversized or too-deep frame *before* building anything from it. Then the tree is
//! turned into Python objects exactly once. Anything with a `"$"` tag takes the already-tested
//! `wire.rs` path, so the tag rules live in one place.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PyMemoryView, PySet,
    PyString, PyTuple, PyType,
};

use super::wire::{fail, Decoder, WireNativeError};

const SAFE_INT: i64 = 1 << 53;
/// The parser accepts a few levels more than the value limit so the *decoder* reports an
/// over-deep value with its own message; this only stops runaway recursion in the parser.
const PARSE_DEPTH_SLACK: usize = 16;

// ---------------------------------------------------------------------------------------------
// the tree
// ---------------------------------------------------------------------------------------------

/// A JSON string. `Units` is only used when the text holds an unpaired surrogate, which UTF-8
/// cannot represent; it keeps the UTF-16 code units so Python can rebuild the exact `str`.
enum Str {
    Plain(String),
    Units(Vec<u16>),
}

enum Node {
    Null,
    Bool(bool),
    Int(i64),
    Big(String),
    Float(f64),
    Str(Str),
    List(Vec<Node>),
    Obj(Vec<(Str, Node)>),
}

#[derive(Clone, Copy)]
enum ParseError {
    Json,
    TooMany,
    Deep,
}

impl ParseError {
    fn message(self) -> &'static str {
        match self {
            ParseError::Json => "frame is not valid JSON",
            ParseError::TooMany => "value has too many nodes",
            ParseError::Deep => "value nested too deeply",
        }
    }
}

type PResult<T> = Result<T, ParseError>;

struct Parser<'a> {
    text: &'a str,
    bytes: &'a [u8],
    at: usize,
    left: usize,
    max_depth: usize,
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while let Some(b' ' | b'\t' | b'\n' | b'\r') = self.bytes.get(self.at) {
            self.at += 1;
        }
    }

    fn expect(&mut self, byte: u8) -> PResult<()> {
        if self.bytes.get(self.at) == Some(&byte) {
            self.at += 1;
            Ok(())
        } else {
            Err(ParseError::Json)
        }
    }

    fn literal(&mut self, word: &str, node: Node) -> PResult<Node> {
        if self.bytes[self.at..].starts_with(word.as_bytes()) {
            self.at += word.len();
            Ok(node)
        } else {
            Err(ParseError::Json)
        }
    }

    fn value(&mut self, depth: usize) -> PResult<Node> {
        if depth > self.max_depth {
            return Err(ParseError::Deep);
        }
        if self.left == 0 {
            return Err(ParseError::TooMany);
        }
        self.left -= 1;
        self.skip_ws();
        match self.bytes.get(self.at) {
            Some(b'n') => self.literal("null", Node::Null),
            Some(b't') => self.literal("true", Node::Bool(true)),
            Some(b'f') => self.literal("false", Node::Bool(false)),
            Some(b'"') => {
                self.at += 1;
                Ok(Node::Str(self.string()?))
            }
            Some(b'[') => {
                self.at += 1;
                self.list(depth)
            }
            Some(b'{') => {
                self.at += 1;
                self.object(depth)
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(ParseError::Json), // includes NaN / Infinity, which the protocol refuses
        }
    }

    fn list(&mut self, depth: usize) -> PResult<Node> {
        let mut items = Vec::new();
        self.skip_ws();
        if self.bytes.get(self.at) == Some(&b']') {
            self.at += 1;
            return Ok(Node::List(items));
        }
        loop {
            items.push(self.value(depth + 1)?);
            self.skip_ws();
            match self.bytes.get(self.at) {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    return Ok(Node::List(items));
                }
                _ => return Err(ParseError::Json),
            }
        }
    }

    fn object(&mut self, depth: usize) -> PResult<Node> {
        let mut entries = Vec::new();
        self.skip_ws();
        if self.bytes.get(self.at) == Some(&b'}') {
            self.at += 1;
            return Ok(Node::Obj(entries));
        }
        loop {
            self.skip_ws();
            self.expect(b'"')?;
            let key = self.string()?;
            self.skip_ws();
            self.expect(b':')?;
            entries.push((key, self.value(depth + 1)?));
            self.skip_ws();
            match self.bytes.get(self.at) {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    return Ok(Node::Obj(entries));
                }
                _ => return Err(ParseError::Json),
            }
        }
    }

    fn number(&mut self) -> PResult<Node> {
        let start = self.at;
        if self.bytes.get(self.at) == Some(&b'-') {
            self.at += 1;
        }
        let digits_from = self.at;
        match self.bytes.get(self.at) {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => {
                while let Some(b'0'..=b'9') = self.bytes.get(self.at) {
                    self.at += 1;
                }
            }
            _ => return Err(ParseError::Json),
        }
        let int_digits = self.at - digits_from;
        let mut is_float = false;
        if self.bytes.get(self.at) == Some(&b'.') {
            is_float = true;
            self.at += 1;
            self.digits1()?;
        }
        if let Some(b'e' | b'E') = self.bytes.get(self.at) {
            is_float = true;
            self.at += 1;
            if let Some(b'+' | b'-') = self.bytes.get(self.at) {
                self.at += 1;
            }
            self.digits1()?;
        }
        let text = &self.text[start..self.at];
        if is_float {
            // Rust and Python agree on the grammar above; `1e400` is `inf` in both.
            text.parse::<f64>()
                .map(Node::Float)
                .map_err(|_| ParseError::Json)
        } else if int_digits <= 18 {
            text.parse::<i64>()
                .map(Node::Int)
                .map_err(|_| ParseError::Json)
        } else {
            Ok(Node::Big(text.to_owned()))
        }
    }

    fn digits1(&mut self) -> PResult<()> {
        let from = self.at;
        while let Some(b'0'..=b'9') = self.bytes.get(self.at) {
            self.at += 1;
        }
        if self.at == from {
            Err(ParseError::Json)
        } else {
            Ok(())
        }
    }

    fn hex4(&mut self) -> PResult<u16> {
        let digits = self
            .bytes
            .get(self.at..self.at + 4)
            .ok_or(ParseError::Json)?;
        let mut value = 0u16;
        for &d in digits {
            let n = match d {
                b'0'..=b'9' => d - b'0',
                b'a'..=b'f' => d - b'a' + 10,
                b'A'..=b'F' => d - b'A' + 10,
                _ => return Err(ParseError::Json),
            };
            value = (value << 4) | n as u16;
        }
        self.at += 4;
        Ok(value)
    }

    /// After the opening quote. Plain text is copied as slices; escapes are decoded; an unpaired
    /// surrogate switches the result to UTF-16 units.
    fn string(&mut self) -> PResult<Str> {
        let mut out = String::new();
        let mut units: Option<Vec<u16>> = None;
        loop {
            let chunk_start = self.at;
            while let Some(&b) = self.bytes.get(self.at) {
                if b == b'"' || b == b'\\' || b < 0x20 {
                    break;
                }
                self.at += 1;
            }
            let chunk = &self.text[chunk_start..self.at];
            match &mut units {
                Some(u) => u.extend(chunk.encode_utf16()),
                None => out.push_str(chunk),
            }
            match self.bytes.get(self.at) {
                Some(b'"') => {
                    self.at += 1;
                    return Ok(match units {
                        Some(u) => Str::Units(u),
                        None => Str::Plain(out),
                    });
                }
                Some(b'\\') => {
                    self.at += 1;
                    let escaped = *self.bytes.get(self.at).ok_or(ParseError::Json)?;
                    self.at += 1;
                    let simple = match escaped {
                        b'"' => Some('"'),
                        b'\\' => Some('\\'),
                        b'/' => Some('/'),
                        b'b' => Some('\u{8}'),
                        b'f' => Some('\u{c}'),
                        b'n' => Some('\n'),
                        b'r' => Some('\r'),
                        b't' => Some('\t'),
                        b'u' => None,
                        _ => return Err(ParseError::Json),
                    };
                    if let Some(c) = simple {
                        push_char(&mut out, &mut units, c);
                        continue;
                    }
                    let first = self.hex4()?;
                    if (0xD800..0xDC00).contains(&first) {
                        let paired = self.bytes.get(self.at..self.at + 2) == Some(b"\\u");
                        if paired {
                            let saved = self.at;
                            self.at += 2;
                            let second = self.hex4()?;
                            if (0xDC00..0xE000).contains(&second) {
                                let c = 0x10000
                                    + (((first as u32) - 0xD800) << 10)
                                    + ((second as u32) - 0xDC00);
                                push_char(
                                    &mut out,
                                    &mut units,
                                    char::from_u32(c).ok_or(ParseError::Json)?,
                                );
                                continue;
                            }
                            self.at = saved; // not a pair: the high half stands alone
                        }
                        push_unit(&out, &mut units, first);
                    } else if (0xDC00..0xE000).contains(&first) {
                        push_unit(&out, &mut units, first);
                    } else {
                        push_char(
                            &mut out,
                            &mut units,
                            char::from_u32(first as u32).ok_or(ParseError::Json)?,
                        );
                    }
                }
                _ => return Err(ParseError::Json), // raw control character, or the end of input
            }
        }
    }
}

fn push_char(out: &mut String, units: &mut Option<Vec<u16>>, c: char) {
    match units {
        Some(u) => {
            let mut buf = [0u16; 2];
            u.extend_from_slice(c.encode_utf16(&mut buf));
        }
        None => out.push(c),
    }
}

fn push_unit(out: &str, units: &mut Option<Vec<u16>>, unit: u16) {
    units
        .get_or_insert_with(|| out.encode_utf16().collect())
        .push(unit);
}

fn parse(data: &[u8], max_nodes: usize, max_depth: usize) -> PResult<Node> {
    let text = std::str::from_utf8(data).map_err(|_| ParseError::Json)?;
    let mut parser = Parser {
        text,
        bytes: text.as_bytes(),
        at: 0,
        left: max_nodes,
        max_depth,
    };
    let node = parser.value(0)?;
    parser.skip_ws();
    if parser.at != parser.bytes.len() {
        return Err(ParseError::Json);
    }
    Ok(node)
}

// ---------------------------------------------------------------------------------------------
// tree -> Python
// ---------------------------------------------------------------------------------------------

fn py_str<'py>(py: Python<'py>, s: &Str) -> PyResult<Bound<'py, PyAny>> {
    match s {
        Str::Plain(text) => Ok(PyString::new(py, text).into_any()),
        Str::Units(units) => {
            let raw: Vec<u8> = units.iter().flat_map(|u| u.to_le_bytes()).collect();
            PyBytes::new(py, &raw).call_method1(
                intern!(py, "decode"),
                (intern!(py, "utf-16-le"), intern!(py, "surrogatepass")),
            )
        }
    }
}

/// The generic conversion: what `json.loads` would have built.
fn to_py<'py>(py: Python<'py>, node: &Node) -> PyResult<Bound<'py, PyAny>> {
    Ok(match node {
        Node::Null => py.None().into_bound(py),
        Node::Bool(b) => PyBool::new(py, *b).to_owned().into_any(),
        Node::Int(i) => i.into_pyobject(py)?.into_any(),
        Node::Big(text) => py
            .get_type::<PyInt>()
            .call1((text.as_str(),))
            .or_else(|_| fail(ParseError::Json.message()))?,
        Node::Float(f) => PyFloat::new(py, *f).into_any(),
        Node::Str(s) => py_str(py, s)?,
        Node::List(items) => {
            let out = PyList::empty(py);
            for item in items {
                out.append(to_py(py, item)?)?;
            }
            out.into_any()
        }
        Node::Obj(entries) => {
            let out = PyDict::new(py);
            for (k, v) in entries {
                out.set_item(py_str(py, k)?, to_py(py, v)?)?;
            }
            out.into_any()
        }
    })
}

fn is_dollar(key: &Str) -> bool {
    matches!(key, Str::Plain(s) if s == "$")
}

// ---------------------------------------------------------------------------------------------
// tree -> decoded Python value (the budgeted, tag-aware conversion)
// ---------------------------------------------------------------------------------------------

impl<'py> Decoder<'py> {
    /// `Decoder::decode`, but over a parsed tree instead of Python objects: the same budgets, the
    /// same verdicts. A tagged object (`"$"`) is rare and subtle, so it is handed to the existing
    /// implementation rather than re-implemented.
    fn decode_node(&self, node: &Node, depth: usize) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        if let Node::Obj(entries) = node {
            if entries.iter().any(|(k, _)| is_dollar(k)) {
                return self.decode(&to_py(py, node)?, depth);
            }
        }
        self.spend()?;
        if depth > self.max_depth {
            return fail("value nested too deeply");
        }
        match node {
            Node::Null => Ok(py.None().into_bound(py)),
            Node::Bool(b) => Ok(PyBool::new(py, *b).to_owned().into_any()),
            Node::Str(s) => py_str(py, s),
            Node::Float(f) => Ok(PyFloat::new(py, *f).into_any()),
            Node::Int(i) if (-SAFE_INT..=SAFE_INT).contains(i) => {
                Ok(i.into_pyobject(py)?.into_any())
            }
            // A plain JSON number past 2**53 is never what the encoder sends: it tags them.
            Node::Int(_) | Node::Big(_) => fail("integer outside the safe range must be tagged"),
            Node::List(items) => {
                let out = PyList::empty(py);
                for item in items {
                    out.append(self.decode_node(item, depth + 1)?)?;
                }
                Ok(out.into_any())
            }
            Node::Obj(entries) => {
                let out = PyDict::new(py);
                for (k, v) in entries {
                    out.set_item(py_str(py, k)?, self.decode_node(v, depth + 1)?)?;
                }
                Ok(out.into_any())
            }
        }
    }
}

/// Parse one frame and decode the value fields of the frame types named in `spec`.
///
/// `spec` maps a frame type (`"result"`) to `(single_keys, multi_keys)`: a key in the first is one
/// encoded value, decoded under its own budget; a key in the second is a list of encoded values
/// decoded under ONE shared budget (a call's arguments). Everything else is plain JSON, as
/// `json.loads` would build it. Never raises anything but `WireNativeError`.
#[pyfunction]
pub fn _wire_loads_decoded<'py>(
    py: Python<'py>,
    data: &[u8],
    spec: &Bound<'py, PyDict>,
    max_nodes: i64,
    max_depth: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let budget = (max_nodes.max(0) as usize).saturating_add(64);
    let tree = py
        .detach(|| parse(data, budget, max_depth + PARSE_DEPTH_SLACK))
        .or_else(|e| fail(e.message()))?;
    let Node::Obj(entries) = &tree else {
        return fail("frame is not a message");
    };
    let kind = entries
        .iter()
        .rev()
        .find(|(k, _)| matches!(k, Str::Plain(s) if s == "t"))
        .and_then(|(_, v)| match v {
            Node::Str(Str::Plain(s)) => Some(s.as_str()),
            _ => None,
        });
    let Some(kind) = kind else {
        return fail("frame is not a message");
    };
    let (single, multi): (Vec<String>, Vec<String>) = match spec.get_item(kind)? {
        Some(pair) => pair.extract()?,
        None => (Vec::new(), Vec::new()),
    };
    let new_decoder = || Decoder::new(py, max_depth, max_nodes);
    let out = PyDict::new(py);
    for (k, v) in entries {
        let name = match k {
            Str::Plain(s) => s.as_str(),
            Str::Units(_) => "",
        };
        let value = if single.iter().any(|s| s == name) {
            new_decoder()?.decode_node(v, 0)?
        } else if let (true, Node::List(items)) = (multi.iter().any(|s| s == name), v) {
            let decoder = new_decoder()?;
            let list = PyList::empty(py);
            for item in items {
                list.append(decoder.decode_node(item, 0)?)?;
            }
            list.into_any()
        } else {
            to_py(py, v)?
        };
        out.set_item(py_str(py, k)?, value)?;
    }
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// Python -> bytes
// ---------------------------------------------------------------------------------------------

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

struct Writer<'py> {
    py: Python<'py>,
    buf: Vec<u8>,
    enc_type: Bound<'py, PyType>,
    datetime: Bound<'py, PyAny>,
    max_depth: usize,
    max_frame: usize,
}

impl<'py> Writer<'py> {
    fn push(&mut self, s: &str) {
        self.buf.extend_from_slice(s.as_bytes());
    }

    fn check_size(&self) -> PyResult<()> {
        if self.buf.len() > self.max_frame {
            return fail(format!(
                "message of {} bytes exceeds the {} byte frame cap",
                self.buf.len(),
                self.max_frame
            ));
        }
        Ok(())
    }

    fn text(&mut self, s: &str) {
        self.buf.push(b'"');
        let bytes = s.as_bytes();
        let mut from = 0;
        for (i, &b) in bytes.iter().enumerate() {
            let esc: Option<&[u8]> = match b {
                b'"' => Some(b"\\\""),
                b'\\' => Some(b"\\\\"),
                b'\n' => Some(b"\\n"),
                b'\r' => Some(b"\\r"),
                b'\t' => Some(b"\\t"),
                0x08 => Some(b"\\b"),
                0x0c => Some(b"\\f"),
                _ => None,
            };
            if esc.is_some() || b < 0x20 {
                self.buf.extend_from_slice(&bytes[from..i]);
                match esc {
                    Some(e) => self.buf.extend_from_slice(e),
                    None => self
                        .buf
                        .extend_from_slice(format!("\\u{:04x}", b).as_bytes()),
                }
                from = i + 1;
            }
        }
        self.buf.extend_from_slice(&bytes[from..]);
        self.buf.push(b'"');
    }

    /// A Python `str`, including one with lone surrogates (written as `\udXXX` escapes).
    fn py_text(&mut self, s: &Bound<'py, PyString>) -> PyResult<()> {
        match s.to_str() {
            Ok(text) => {
                self.text(text);
                Ok(())
            }
            Err(_) => {
                let py = self.py;
                let raw = s.call_method1(
                    intern!(py, "encode"),
                    (intern!(py, "utf-16-le"), intern!(py, "surrogatepass")),
                )?;
                let raw = raw.cast::<PyBytes>()?;
                self.buf.push(b'"');
                let bytes = raw.as_bytes();
                let mut units = (0..bytes.len() / 2)
                    .map(|i| u16::from_le_bytes([bytes[2 * i], bytes[2 * i + 1]]))
                    .peekable();
                while let Some(u) = units.next() {
                    let decoded = if (0xD800..0xDC00).contains(&u) {
                        match units.peek() {
                            Some(&low) if (0xDC00..0xE000).contains(&low) => {
                                units.next();
                                char::decode_utf16([u, low]).next().and_then(|r| r.ok())
                            }
                            _ => None,
                        }
                    } else {
                        char::from_u32(u as u32)
                    };
                    match decoded {
                        Some(c) if c != '"' && c != '\\' && c >= ' ' => {
                            let mut b = [0u8; 4];
                            self.buf.extend_from_slice(c.encode_utf8(&mut b).as_bytes());
                        }
                        Some(c) => self.text_escape_char(c),
                        None => self
                            .buf
                            .extend_from_slice(format!("\\u{:04x}", u).as_bytes()),
                    }
                }
                self.buf.push(b'"');
                Ok(())
            }
        }
    }

    fn text_escape_char(&mut self, c: char) {
        let mut b = [0u8; 4];
        let one = c.encode_utf8(&mut b);
        let start = self.buf.len();
        self.text(one);
        // `text` wrapped it in quotes; keep the escaped body only
        self.buf.remove(start);
        self.buf.pop();
    }

    fn int(&mut self, v: &Bound<'py, PyAny>) -> PyResult<()> {
        match v.extract::<i64>() {
            Ok(n) => self.push(&n.to_string()),
            Err(_) => self.push(&v.str()?.to_cow()?),
        }
        Ok(())
    }

    fn float(&mut self, f: f64) -> PyResult<()> {
        if !f.is_finite() {
            return fail("Out of range float values are not JSON compliant");
        }
        self.push(&format!("{f:?}"));
        Ok(())
    }

    fn tag_open(&mut self, tag: &str) {
        self.push("{\"$\":\"");
        self.push(tag);
        self.push("\"");
    }

    /// A plain JSON-able Python value, with `Enc(...)` markers encoded as wire values.
    fn plain(&mut self, v: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        if depth > self.max_depth + PARSE_DEPTH_SLACK {
            return fail("message nested too deeply");
        }
        if v.is_instance(&self.enc_type)? {
            let inner = v.getattr(intern!(self.py, "value"))?;
            return self.encoded(&inner, 0);
        }
        if v.is_none() {
            self.push("null");
        } else if v.is_instance_of::<PyBool>() {
            self.push(if v.is_truthy()? { "true" } else { "false" });
        } else if v.is_instance_of::<PyInt>() {
            self.int(v)?;
        } else if v.is_instance_of::<PyFloat>() {
            self.float(v.extract()?)?;
        } else if let Ok(s) = v.cast::<PyString>() {
            self.py_text(s)?;
        } else if v.is_instance_of::<PyList>() || v.is_instance_of::<PyTuple>() {
            self.buf.push(b'[');
            for (i, item) in v.try_iter()?.enumerate() {
                if i > 0 {
                    self.buf.push(b',');
                }
                self.plain(&item?, depth + 1)?;
                self.check_size()?;
            }
            self.buf.push(b']');
        } else if let Ok(d) = v.cast::<PyDict>() {
            self.buf.push(b'{');
            for (i, (k, val)) in d.iter().enumerate() {
                if i > 0 {
                    self.buf.push(b',');
                }
                let Ok(k) = k.cast_into::<PyString>() else {
                    return fail("message keys must be strings");
                };
                self.py_text(&k)?;
                self.buf.push(b':');
                self.plain(&val, depth + 1)?;
                self.check_size()?;
            }
            self.buf.push(b'}');
        } else {
            return fail(format!("{} is not JSON serializable", v.get_type().name()?));
        }
        Ok(())
    }

    /// The tagged form of a Python value (what `_wire.encode_value` builds), written directly.
    fn encoded(&mut self, v: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        let py = self.py;
        if depth > self.max_depth {
            return fail("value nested too deeply to cross the isolation boundary");
        }
        if v.is_none() {
            self.push("null");
        } else if v.is_instance_of::<PyBool>() {
            self.push(if v.is_truthy()? { "true" } else { "false" });
        } else if let Ok(s) = v.cast::<PyString>() {
            self.py_text(s)?;
        } else if v.is_instance_of::<PyInt>() {
            match v.extract::<i64>() {
                Ok(n) if (-SAFE_INT..=SAFE_INT).contains(&n) => self.push(&n.to_string()),
                _ => {
                    self.tag_open("int");
                    self.push(",\"v\":\"");
                    self.int(v)?;
                    self.push("\"}");
                }
            }
        } else if v.is_instance_of::<PyFloat>() {
            let f: f64 = v.extract()?;
            if f.is_nan() {
                self.push("{\"$\":\"f\",\"v\":\"nan\"}");
            } else if f.is_infinite() {
                self.push(if f > 0.0 {
                    "{\"$\":\"f\",\"v\":\"inf\"}"
                } else {
                    "{\"$\":\"f\",\"v\":\"-inf\"}"
                });
            } else if f == 0.0 && f.is_sign_negative() {
                self.push("{\"$\":\"f\",\"v\":\"-0\"}");
            } else {
                self.float(f)?;
            }
        } else if let Some(bytes) = self.byte_like(v)? {
            self.tag_open("b");
            self.push(",\"v\":\"");
            for chunk in bytes.chunks(3) {
                let n = (chunk[0] as u32) << 16
                    | (*chunk.get(1).unwrap_or(&0) as u32) << 8
                    | *chunk.get(2).unwrap_or(&0) as u32;
                self.buf.push(B64[(n >> 18) as usize & 63]);
                self.buf.push(B64[(n >> 12) as usize & 63]);
                self.buf.push(if chunk.len() > 1 {
                    B64[(n >> 6) as usize & 63]
                } else {
                    b'='
                });
                self.buf.push(if chunk.len() > 2 {
                    B64[n as usize & 63]
                } else {
                    b'='
                });
            }
            self.push("\"}");
        } else if v.is_instance_of::<super::python::JsUndefined>() {
            self.push("{\"$\":\"u\"}");
        } else if v.is_instance(&self.datetime)? {
            self.tag_open("dt");
            self.push(",\"v\":");
            let iso = v.call_method0(intern!(py, "isoformat"))?;
            self.py_text(iso.cast::<PyString>()?)?;
            self.push("}");
        } else if v.is_instance_of::<PyList>() || v.is_instance_of::<PyTuple>() {
            self.items(v, depth)?;
        } else if v.is_instance_of::<PySet>() || v.is_instance_of::<PyFrozenSet>() {
            self.tag_open("set");
            self.push(",\"v\":");
            self.items(v, depth)?;
            self.push("}");
        } else if let Ok(d) = v.cast::<PyDict>() {
            let mut plain_keys = true;
            for (k, _) in d.iter() {
                if !k.is_instance_of::<PyString>() || k.eq("$")? {
                    plain_keys = false;
                    break;
                }
            }
            if plain_keys {
                self.buf.push(b'{');
                for (i, (k, val)) in d.iter().enumerate() {
                    if i > 0 {
                        self.buf.push(b',');
                    }
                    self.py_text(k.cast::<PyString>()?)?;
                    self.buf.push(b':');
                    self.encoded(&val, depth + 1)?;
                    self.check_size()?;
                }
                self.buf.push(b'}');
            } else {
                self.tag_open("d");
                self.push(",\"v\":[");
                for (i, (k, val)) in d.iter().enumerate() {
                    if i > 0 {
                        self.buf.push(b',');
                    }
                    self.buf.push(b'[');
                    self.encoded(&k, depth + 1)?;
                    self.buf.push(b',');
                    self.encoded(&val, depth + 1)?;
                    self.buf.push(b']');
                    self.check_size()?;
                }
                self.push("]}");
            }
        } else {
            return fail(format!(
                "{} cannot cross the isolation boundary",
                v.get_type().name()?
            ));
        }
        Ok(())
    }

    fn items(&mut self, v: &Bound<'py, PyAny>, depth: usize) -> PyResult<()> {
        self.buf.push(b'[');
        for (i, item) in v.try_iter()?.enumerate() {
            if i > 0 {
                self.buf.push(b',');
            }
            self.encoded(&item?, depth + 1)?;
            self.check_size()?;
        }
        self.buf.push(b']');
        Ok(())
    }

    fn byte_like(&self, v: &Bound<'py, PyAny>) -> PyResult<Option<Vec<u8>>> {
        if let Ok(b) = v.cast::<PyBytes>() {
            return Ok(Some(b.as_bytes().to_vec()));
        }
        if v.is_instance_of::<PyByteArray>() || v.is_instance_of::<PyMemoryView>() {
            let raw = self.py.get_type::<PyBytes>().call1((v,))?;
            return Ok(Some(raw.cast::<PyBytes>()?.as_bytes().to_vec()));
        }
        Ok(None)
    }
}

/// Serialize `message` (a dict of plain JSON-able values) with every `enc_type` instance written
/// as a wire value. One pass, straight to bytes.
#[pyfunction]
pub fn _wire_dumps<'py>(
    py: Python<'py>,
    message: &Bound<'py, PyAny>,
    enc_type: &Bound<'py, PyType>,
    max_depth: usize,
    max_frame: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    let mut writer = Writer {
        py,
        buf: Vec::with_capacity(256),
        enc_type: enc_type.clone(),
        datetime: py.import("datetime")?.getattr("datetime")?,
        max_depth,
        max_frame,
    };
    writer.plain(message, 0)?;
    writer.check_size()?;
    Ok(PyBytes::new(py, &writer.buf))
}

#[allow(dead_code)]
fn _assert_exception_type_is_used(_: &WireNativeError) {}
