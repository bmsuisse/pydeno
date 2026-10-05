//! Unit tests of the pure scanner. The differential tests against the Python reference are in
//! `tests/test_scanner_differential.py`.

use super::*;

fn points(s: &str) -> Vec<u32> {
    s.chars().map(|c| c as u32).collect()
}

fn cls() -> Classes {
    Classes::for_version(Classes::versions()[0]).expect("a version")
}

fn strict() -> Policy {
    let mut p = Policy::new(vec![], vec![points("secretTool")]);
    p.forbid_eval = true;
    p.forbid_function = true;
    p.forbid_dynamic_import = true;
    p.forbid_webassembly = true;
    p.forbid_computed_global_access = true;
    p
}

fn codes(code: &str, policy: Option<&Policy>) -> Vec<u8> {
    scan(&points(code), policy, Options::default(), cls())
        .iter()
        .map(|h| h.code)
        .collect()
}

#[test]
fn every_version_classifies_ascii_like_python() {
    for v in Classes::versions() {
        let c = Classes::for_version(v).unwrap();
        assert!(c.is_word('a' as u32) && c.is_word('_' as u32) && !c.is_word('$' as u32));
        assert!(c.is_space(' ' as u32) && c.is_space(0x1C) && !c.is_space('x' as u32));
        assert!(c.is_digit('7' as u32) && c.is_id_char('$' as u32));
        assert!(c.is_word(0xE9) && c.is_space(0x2028) && !c.is_word(0xD800));
    }
    assert!(Classes::for_version("0.0.0").is_none());
}

#[test]
fn hidden_eval_is_found_in_the_default_mode() {
    let p = strict();
    for code in [
        "await /`/\neval(\"1\")\n// `",
        "globalThis[\"\\145val\"](\"1\")",
        "\\u{0000000065}val(\"1\")",
        "//x\\\neval(\"1\")",
        "with (()=>0) { constructor(\"return 6*7\")() }",
    ] {
        let found = codes(code, Some(&p));
        assert!(
            found.contains(&FORBIDDEN_EVAL) || found.contains(&FORBIDDEN_FUNCTION_CONSTRUCTOR),
            "{code:?}: {found:?}"
        );
    }
}

#[test]
fn a_constructor_definition_is_allowed() {
    let mut p = Policy::new(vec![], vec![]);
    p.forbid_function = true;
    assert!(codes("class A { constructor(a) { this.a = a } }", Some(&p)).is_empty());
    assert_eq!(
        codes("constructor('x')", Some(&p)),
        vec![FORBIDDEN_FUNCTION_CONSTRUCTOR]
    );
}

#[test]
fn the_findings_per_reading_are_capped() {
    let p = strict();
    let code = "eval;".repeat(5000);
    let n = codes(&code, Some(&p)).len();
    assert_eq!(n, text::MAX_POLICY_FINDINGS);
}

#[test]
fn usability_rules_without_a_policy() {
    let found = codes(
        "import x from 'y'; require('fs'); globalThis['fetch']()",
        None,
    );
    assert_eq!(found, vec![STATIC_IMPORT, NAMED, GLOBAL_ALIAS_KEY]);
}

#[test]
fn positions_count_newlines_only() {
    let code = points("a\r\nb\u{2028}eval");
    let mut p = Policy::new(vec![], vec![]);
    p.forbid_eval = true;
    let hits = scan(&code, Some(&p), Options::default(), cls());
    assert_eq!(positions(&code, &hits), vec![(2, 3)]);
}

/// A tiny deterministic generator, so this test needs no extra crate.
struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u32 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) as u32
    }
}

#[test]
fn no_input_panics() {
    const PIECES: [&str; 24] = [
        "\\",
        "u{",
        "}",
        "u",
        "x4",
        "0",
        "7",
        "/",
        "[",
        "]",
        "`",
        "${",
        "'",
        "\"",
        "(",
        ")",
        "{",
        "\n",
        "\r",
        "constructor",
        "eval",
        "import",
        "globalThis",
        "//",
    ];
    let mut rng = Lcg(7);
    let mut precise = strict();
    precise.ignore_strings_and_comments = true;
    let mut rules = strict();
    rules.include_preflight_rules = true;
    for _ in 0..3000 {
        let len = rng.next() % 40;
        let mut code: Vec<u32> = Vec::new();
        for _ in 0..len {
            match rng.next() % 4 {
                0 => code.push(rng.next() % 0x110000), // any code point, surrogates included
                _ => code.extend(points(PIECES[rng.next() as usize % PIECES.len()])),
            }
        }
        for v in Classes::versions() {
            let c = Classes::for_version(v).unwrap();
            for policy in [None, Some(&strict()), Some(&precise), Some(&rules)] {
                let hits = scan(&code, policy, Options::default(), c);
                assert_eq!(positions(&code, &hits).len(), hits.len());
            }
        }
    }
}

#[test]
fn deep_nesting_does_not_recurse() {
    let mut precise = strict();
    precise.ignore_strings_and_comments = true;
    for code in [
        "(".repeat(1 << 20),
        "{".repeat(1 << 20),
        "`${".repeat(1 << 18),
        format!("constructor{}", "(".repeat(1 << 20)),
    ] {
        let code = points(&code);
        scan(&code, Some(&strict()), Options::default(), cls());
        scan(&code, Some(&precise), Options::default(), cls());
        scan(&code, None, Options::default(), cls());
    }
}
