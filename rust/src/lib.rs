#[derive(Debug, PartialEq, Eq, Clone)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Option<String>,
    pub build: Option<String>,
}

fn compare_prerelease(a: &Option<String>, b: &Option<String>) -> std::cmp::Ordering {
    match (a, b) {
        (None, None) => std::cmp::Ordering::Equal,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (Some(_), None) => std::cmp::Ordering::Less,
        (Some(a), Some(b)) => {
            let a_parts: Vec<&str> = a.split('.').collect();
            let b_parts: Vec<&str> = b.split('.').collect();
            for (p1, p2) in a_parts.iter().zip(b_parts.iter()) {
                let ord = compare_part(p1, p2);
                if ord != std::cmp::Ordering::Equal {
                    return ord;
                }
            }
            a_parts.len().cmp(&b_parts.len())
        }
    }
}

fn compare_part(a: &str, b: &str) -> std::cmp::Ordering {
    let a_numeric = a.bytes().all(|byte| byte.is_ascii_digit());
    let b_numeric = b.bytes().all(|byte| byte.is_ascii_digit());
    match (a_numeric, b_numeric) {
        (true, true) => a.len().cmp(&b.len()).then(a.cmp(b)),
        (true, false) => std::cmp::Ordering::Less,
        (false, true) => std::cmp::Ordering::Greater,
        (false, false) => a.cmp(b),
    }
}

fn valid_core_number(value: &str) -> bool {
    !value.is_empty()
        && value.bytes().all(|byte| byte.is_ascii_digit())
        && (value == "0" || !value.starts_with('0'))
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
}

fn valid_prerelease(value: &str) -> bool {
    !value.is_empty()
        && value.split('.').all(|identifier| {
            valid_identifier(identifier)
                && !(identifier.len() > 1
                    && identifier.bytes().all(|byte| byte.is_ascii_digit())
                    && identifier.starts_with('0'))
        })
}

fn valid_build(value: &str) -> bool {
    !value.is_empty() && value.split('.').all(valid_identifier)
}

pub fn parse(s: &str) -> Result<Version, String> {
    let (main_and_prerelease, build) = match s.split_once('+') {
        Some((left, right)) if !right.contains('+') && valid_build(right) => {
            (left, Some(right))
        }
        Some(_) => return Err("invalid build metadata".to_string()),
        None => (s, None),
    };

    let (main, prerelease) = match main_and_prerelease.split_once('-') {
        Some((left, right)) if valid_prerelease(right) => (left, Some(right)),
        Some(_) => return Err("invalid prerelease".to_string()),
        None => (main_and_prerelease, None),
    };

    let mut main_parts = main.split('.');
    let major_text = main_parts.next().unwrap_or("");
    let minor_text = main_parts.next().unwrap_or("");
    let patch_text = main_parts.next().unwrap_or("");
    if main_parts.next().is_some()
        || !valid_core_number(major_text)
        || !valid_core_number(minor_text)
        || !valid_core_number(patch_text)
    {
        return Err("invalid core version".to_string());
    }

    let major = major_text
        .parse()
        .map_err(|_| "major version is out of range".to_string())?;
    let minor = minor_text
        .parse()
        .map_err(|_| "minor version is out of range".to_string())?;
    let patch = patch_text
        .parse()
        .map_err(|_| "patch version is out of range".to_string())?;

    Ok(Version {
        major,
        minor,
        patch,
        prerelease: prerelease.map(str::to_string),
        build: build.map(str::to_string),
    })
}

pub fn to_string(v: &Version) -> String {
    let mut s = format!("{}.{}.{}", v.major, v.minor, v.patch);
    if let Some(ref p) = v.prerelease {
        s.push('-');
        s.push_str(p);
    }
    if let Some(ref b) = v.build {
        s.push('+');
        s.push_str(b);
    }
    s
}

pub fn compare(a: &Version, b: &Version) -> std::cmp::Ordering {
    a.major
        .cmp(&b.major)
        .then(a.minor.cmp(&b.minor))
        .then(a.patch.cmp(&b.patch))
        .then(compare_prerelease(&a.prerelease, &b.prerelease))
}

pub fn bump_major(v: &Version) -> Version {
    Version {
        major: v.major + 1,
        minor: 0,
        patch: 0,
        prerelease: None,
        build: None,
    }
}

pub fn bump_minor(v: &Version) -> Version {
    Version {
        major: v.major,
        minor: v.minor + 1,
        patch: 0,
        prerelease: None,
        build: None,
    }
}

pub fn bump_patch(v: &Version) -> Version {
    Version {
        major: v.major,
        minor: v.minor,
        patch: v.patch + 1,
        prerelease: None,
        build: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn version(prerelease: Option<&str>, build: Option<&str>) -> Version {
        Version {
            major: 1,
            minor: 2,
            patch: 3,
            prerelease: prerelease.map(str::to_string),
            build: build.map(str::to_string),
        }
    }

    #[test]
    fn public_parse_signature_is_fixed() {
        let _: fn(&str) -> Result<Version, String> = parse;
    }

    #[test]
    fn parses_and_formats_complete_versions() {
        match parse("1.2.3-alpha.1+build.0001") {
            Ok(parsed) => {
                assert_eq!(parsed, version(Some("alpha.1"), Some("build.0001")));
                assert_eq!(to_string(&parsed), "1.2.3-alpha.1+build.0001");
            }
            Err(error) => assert!(false, "unexpected parse error: {error}"),
        }
    }

    #[test]
    fn rejects_invalid_semver_syntax() {
        let invalid = [
            "1.0",
            "01.0.0",
            "1.01.0",
            "1.0.01",
            "1.0.0-",
            "1.0.0+",
            "1.0.0-01",
            "1.0.0-alpha..1",
            "1.0.0-alpha_beta",
            "1.0.0+a..b",
            "1.0.0+build+again",
            "1.0.0-α",
        ];
        for input in invalid {
            assert!(parse(input).is_err(), "accepted invalid version: {input}");
        }
    }

    #[test]
    fn allows_leading_zeroes_in_build_identifiers() {
        assert!(parse("1.0.0+000.01").is_ok());
    }

    #[test]
    fn follows_the_semver_precedence_chain() {
        let chain = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ];
        for pair in chain.windows(2) {
            match (parse(pair[0]), parse(pair[1])) {
                (Ok(left), Ok(right)) => {
                    assert_eq!(compare(&left, &right), std::cmp::Ordering::Less)
                }
                _ => assert!(false, "precedence fixture did not parse"),
            }
        }
    }

    #[test]
    fn compares_numeric_prerelease_identifiers_without_integer_bounds() {
        let smaller = version(Some("999999999999999999999"), None);
        let larger = version(Some("1000000000000000000000"), None);
        assert_eq!(compare(&smaller, &larger), std::cmp::Ordering::Less);
    }

    #[test]
    fn ignores_build_metadata_for_precedence() {
        let left = version(None, Some("build.1"));
        let right = version(None, Some("build.999"));
        assert_eq!(compare(&left, &right), std::cmp::Ordering::Equal);
    }

    #[test]
    fn bumps_reset_lower_components_and_metadata() {
        let original = version(Some("rc.1"), Some("build.5"));
        assert_eq!(bump_major(&original), Version {
            major: 2,
            minor: 0,
            patch: 0,
            prerelease: None,
            build: None,
        });
        assert_eq!(bump_minor(&original), Version {
            major: 1,
            minor: 3,
            patch: 0,
            prerelease: None,
            build: None,
        });
        assert_eq!(bump_patch(&original), Version {
            major: 1,
            minor: 2,
            patch: 4,
            prerelease: None,
            build: None,
        });
    }
}
