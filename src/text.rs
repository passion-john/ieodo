//! Text helpers shared by storage and search: Korean-aware tokenizing,
//! slugs, and secret redaction before anything reaches the database.

use lindera::segmenter::Segmenter;
use regex::Regex;
use std::borrow::Cow;
use std::sync::OnceLock;

static SEGMENTER: OnceLock<Option<Segmenter>> = OnceLock::new();
static SECRET_RULES: OnceLock<Vec<(Regex, &'static str)>> = OnceLock::new();
static ENV_ASSIGNMENT: OnceLock<Regex> = OnceLock::new();

fn segmenter() -> Option<&'static Segmenter> {
    SEGMENTER
        .get_or_init(|| {
            let config = serde_json::json!({"dictionary": "embedded://ko-dic", "mode": "normal"});
            Segmenter::from_config(&config)
                .map_err(|e| eprintln!("ieodo: ko-dic unavailable, Korean search degrades: {e}"))
                .ok()
        })
        .as_ref()
}

/// Particles, endings, punctuation and affixes carry no search meaning alone.
fn functional_pos(pos: &str) -> bool {
    pos.starts_with('J')
        || pos.starts_with('E')
        || (pos.starts_with('S') && !matches!(pos, "SL" | "SN" | "SH"))
        || pos.starts_with("XS")
        || matches!(pos, "XPN" | "VCP" | "VCN" | "VX" | "NNB")
}

fn is_hangul(c: char) -> bool {
    ('\u{AC00}'..='\u{D7A3}').contains(&c)
}

/// Tokens for the FTS index and for queries. Version strings such as
/// `7.1.4` or `rc.15` stay whole; code identifiers are also split into parts.
pub fn tokenize(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    if text.chars().any(is_hangul)
        && let Some(segmenter) = segmenter()
        && let Ok(mut tokens) = segmenter.segment(Cow::Borrowed(text))
    {
        for token in &mut tokens {
            let surface = token.surface.trim().to_lowercase();
            if !surface.chars().any(is_hangul) {
                continue;
            }
            let details = token.details();
            let pos = details.first().copied().unwrap_or("UNK");
            if !functional_pos(pos) && (surface.chars().count() >= 2 || pos.starts_with("NN")) {
                out.push(surface);
            }
            // Compound nouns ("형태소/NNG/*+분석기/NNG/*") also index their parts.
            if let Some(expression) = details.get(7).filter(|e| e.contains('+')) {
                for part in expression.split('+') {
                    let mut fields = part.split('/');
                    if let (Some(sub), Some(sub_pos)) = (fields.next(), fields.next())
                        && !functional_pos(sub_pos)
                        && sub.chars().count() >= 2
                    {
                        out.push(sub.to_lowercase());
                    }
                }
            }
        }
    }
    // Hangul ends a word too, so "libSQL은" still indexes "libsql".
    for word in
        text.split(|c: char| is_hangul(c) || !(c.is_alphanumeric() || matches!(c, '_' | '-' | '.')))
    {
        let word = word.trim_matches(|c: char| matches!(c, '_' | '-' | '.'));
        if word.is_empty() {
            continue;
        }
        let lower = word.to_lowercase();
        if lower.chars().count() >= 2 || lower.chars().all(|c| c.is_ascii_digit()) {
            out.push(lower.clone());
        }
        split_identifier(word, &mut out);
    }
    out.sort();
    out.dedup();
    out
}

/// `snake_case`, `kebab-case`, `dot.case` and `camelCase` parts.
fn split_identifier(word: &str, out: &mut Vec<String>) {
    let mut part = String::new();
    let mut prev_lower = false;
    let flush = |part: &mut String, out: &mut Vec<String>| {
        if part.chars().count() >= 2 && part.len() < word.len() {
            out.push(part.to_lowercase());
        }
        part.clear();
    };
    for c in word.chars() {
        if matches!(c, '_' | '-' | '.') {
            flush(&mut part, out);
            prev_lower = false;
        } else {
            if c.is_uppercase() && prev_lower {
                flush(&mut part, out);
            }
            prev_lower = c.is_lowercase();
            part.push(c);
        }
    }
    flush(&mut part, out);
}

/// Replaces credentials with `[REDACTED_…]` markers. Returns the redaction count.
pub fn redact(input: &str) -> (String, usize) {
    let rules =
        SECRET_RULES.get_or_init(|| {
            [
            (r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b", "[REDACTED_GITHUB_TOKEN]"),
            (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "[REDACTED_GITHUB_PAT]"),
            (r"\bsk-(?:proj-|ant-|or-v1-|live-|test-)?[A-Za-z0-9_-]{20,}\b", "[REDACTED_API_KEY]"),
            (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED_AWS_KEY]"),
            (r"\bAIza[0-9A-Za-z_-]{35}\b", "[REDACTED_GOOGLE_API_KEY]"),
            (
                r"\beyJ[A-Za-z0-9_-]{12,}\.eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b",
                "[REDACTED_JWT]",
            ),
            (r"\bxox[baprs]-[A-Za-z0-9-]{15,}\b", "[REDACTED_SLACK_TOKEN]"),
            (
                r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                "[REDACTED_PRIVATE_KEY]",
            ),
        ]
        .into_iter()
        .filter_map(|(pattern, mark)| Regex::new(pattern).ok().map(|re| (re, mark)))
        .collect()
        });
    let mut text = input.to_owned();
    let mut count = 0;
    for (re, mark) in rules {
        let hits = re.find_iter(&text).count();
        if hits > 0 {
            count += hits;
            text = re.replace_all(&text, *mark).into_owned();
        }
    }
    let assignment = ENV_ASSIGNMENT.get_or_init(|| {
        Regex::new(
            r#"(?i)(["']?\b(?:[A-Z0-9]+_)*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|AUTH_KEY|ACCESS_KEY)(?:_KEY)?\b["']?\s*[:=]\s*["']?)([^\s"'`\],;]{6,})"#,
        )
        .expect("static secret assignment pattern")
    });
    let replaced = assignment.replace_all(&text, |caps: &regex::Captures| {
        let value = &caps[2];
        if value.starts_with("[REDACTED") || value.starts_with('$') {
            caps[0].to_owned()
        } else {
            count += 1;
            format!("{}[REDACTED_SECRET]", &caps[1])
        }
    });
    (replaced.into_owned(), count)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn korean_tokens_drop_particles_and_keep_nouns() {
        let tokens = tokenize("인형이 맞았을 때 반응을 보여준다");
        assert!(tokens.contains(&"인형".to_owned()), "{tokens:?}");
        assert!(tokens.contains(&"반응".to_owned()), "{tokens:?}");
        assert!(!tokens.iter().any(|t| t == "이" || t == "을"), "{tokens:?}");
    }

    #[test]
    fn versions_stay_whole_and_identifiers_split() {
        let tokens =
            tokenize("fastembed 7.1.4 uses ort 2.0.0-rc.15 via TextEmbedding and gpt_research");
        for expected in [
            "7.1.4",
            "2.0.0-rc.15",
            "textembedding",
            "text",
            "embedding",
            "gpt_research",
            "research",
        ] {
            assert!(
                tokens.contains(&expected.to_owned()),
                "{expected} missing from {tokens:?}"
            );
        }
    }

    #[test]
    fn latin_words_with_korean_particles_are_indexed() {
        let tokens = tokenize("libSQL은 SQLite의 포크이고 v0.3.0을 쓴다");
        for expected in ["libsql", "sqlite", "v0.3.0", "포크"] {
            assert!(
                tokens.contains(&expected.to_owned()),
                "{expected} missing from {tokens:?}"
            );
        }
    }

    #[test]
    fn secrets_are_redacted_but_env_references_kept() {
        let (text, n) = redact(
            "token ghp_abcdefghijklmnopqrstuvwxyz1234 and API_KEY=supersecret123 and TOKEN=$HOME_TOKEN",
        );
        assert_eq!(n, 2, "{text}");
        assert!(!text.contains("ghp_abc"));
        assert!(!text.contains("supersecret123"));
        assert!(text.contains("$HOME_TOKEN"));
    }

    #[test]
    fn ordinary_words_that_contain_token_or_secret_are_kept() {
        for plain in [
            "tokenizer: lindera ko-dic",
            "max_tokens: 128000",
            "secrets: none here",
            "token_count: 128000",
            "secret_name: production-db",
        ] {
            assert_eq!(redact(plain), (plain.to_owned(), 0), "{plain}");
        }
        for (secret, value) in [
            ("IEODO_TURSO_TOKEN=eyJhbGciOiJIUzI1NiJ9abc", "eyJhbGci"),
            ("access_token: abcdef123456", "abcdef123456"),
            ("DB_PASSWORD = hunter2hunter2", "hunter2"),
        ] {
            let (text, n) = redact(secret);
            assert_eq!(n, 1, "{text}");
            assert!(!text.contains(value), "{text}");
        }
    }
}
