use std::cmp::Ordering;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Option<String>,
    pub build: Option<String>,
}

fn is_numeric(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

fn validate_identifier(s: &str, is_prerelease: bool) -> Result<(), String> {
    if s.is_empty() {
        return Err("empty identifier".into());
    }
    for b in s.bytes() {
        let ok = (b'a'..=b'z').contains(&b)
            || (b'A'..=b'Z').contains(&b)
            || (b'0'..=b'9').contains(&b)
            || b == b'-';
        if !ok {
            return Err("invalid character in identifier".into());
        }
    }
    if is_prerelease && is_numeric(s) && s.starts_with('0') && s.len() > 1 {
        return Err("numeric identifier cannot have leading zero".into());
    }
    Ok(())
}

fn parse_component(s: &str) -> Result<u64, String> {
    if s.is_empty() {
        return Err("empty numeric component".into());
    }
    if s.starts_with('0') && s.len() >  1 {
        return Err("numeric component cannot have leading zero".into());
    }
    for b in s.bytes() {
        if !b.is_ascii_digit() {
            return Err("non-digit in numeric component".into());
        }
    }
    s.parse::<u64>().map_err(|e| e.to_string())
}

pub fn parse(s: &str) -> Result<Version, String> {
    if s.is_empty() {
        return Err("empty string".into());
    }

    // Split build metadata first (+)
    let (version_and_pre, build) = match s.split_once('+') {
        Some((v, b)) => {
            if b.is_empty() {
                return Err("empty build metadata".into());
            }
            // validate build identifiers separated by dots
            for part in b.split('.') {
                if part.is_empty() {
                    return Err("empty build identifier".into());
                }
                for byte in part.bytes() {
                    let ok = (b'a'..=b'z').contains(&byte)
                        || (b'A'..=b'Z').contains(&byte)
                        || (b'0'..=b'9').contains(&byte)
                        || byte == b'-';
                    if !ok {
                        return Err("invalid character in build metadata".into());
                    }
                }
            }
            (v, Some(b.to_string()))
        }
        None => (s, None),
    };

    // Split prerelease next (-)
    let (version_core, prerelease) = match version_and_pre.split_once('-') {
        Some((v, p)) => {
            if p.is_empty() {
                return Err("empty prerelease".into());
            }
            for part in p.split('.') {
                validate_identifier(part, true)?;
            }
            (v, Some(p.to_string()))
        }
        None => (version_and_pre, None),
    };

    let parts: Vec<&str> = version_core.split('.').collect();
    if parts.len() != 3 {
        return Err("version must have major, minor, patch".into());
    }

    let major = parse_component(parts[0])?;
    let minor = parse_component(parts[1])?;
    let patch = parse_component(parts[2])?;

    Ok(Version {
        major,
        minor,
        patch,
        prerelease,
        build,
    })
}

pub fn to_string(v: &Version) -> String {
    let mut out = format!("{}.{}.{}", v.major, v.minor, v.patch);
    if let Some(ref pre) = v.prerelease {
        out.push('-');
        out.push_str(pre);
    }
    if let Some(ref build) = v.build {
        out.push('+');
        out.push_str(build);
    }
    out
}

fn compare_identifiers(a: &str, b: &str) -> Ordering {
    let a_num = is_numeric(a);
    let b_num = is_numeric(b);

    if a_num && b_num {
        // Compare numerically by length first, then lexically
        if a.len() != b.len() {
            return a.len().cmp(&b.len());
        }
        return a.cmp(b);
    } else if a_num {
        Ordering::Less
    } else if b_num {
        Ordering::Greater
    } else {
        a.cmp(b)
    }
}

pub fn compare(a: &Version, b: &Version) -> Ordering {
    match a.major.cmp(&b.major) {
        Ordering::Equal => {}
        ord => return ord,
    }
    match a.minor.cmp(&b.minor) {
        Ordering::Equal => {}
        ord => return ord,
    }
    match a.patch.cmp(&b.patch) {
        Ordering::Equal => {}
        ord => return ord,
    }

    match (&a.prerelease, &b.prerelease) {
        (None, None) => Ordering::Equal,
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (Some(ap), Some(bp)) => {
            let a_parts: Vec<&str> = ap.split('.').collect();
            let b_parts: Vec<&str> = bp.split('.').collect();

            for (part_a, part_b) in a_parts.iter().zip(b_parts.iter()) {
                let ord = compare_identifiers(part_a, part_b);
                if ord != Ordering::Equal {
                    return ord;
                }
            }

            a_parts.len().cmp(&b_parts.len())
        }
    }
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
    // Check if there is a prerelease and if the core version has no prerelease components, etc.
    // Wait, reference/version.py: bump_patch increments patch if no prerelease, or if prerelease exists... let's check standard semver bump_patch behavior.
    // In python semver / reference implementation, bump_patch usually:
    // If v.prerelease is present and patch is being bumped... wait, does bump_patch drop prerelease or preserve it if prerelease is already present?
    // Let's check Python reference/version.py bump_patch behavior or standard semver.
    // Actually, rule from prompt says: "bump_patch increments patch; drop prerelease and build... Wait, bump_patch increments patch and drops prerelease/build unless... Wait, let's check what reference/version.py does."
    // Since we don't have direct access to reference/version.py content except the prompt rules:
    // "bump_major increments major and zeros minor/patch; bump_minor increments minor and zeros patch; bump_patch increments patch. All three drop metadata."
    // Wait! "All three drop metadata." That's extremely clear and direct from the prompt instructions!
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

    #[test]
    fn test_parse_valid() {
        let v = parse("1.2.3-alpha.1+build.123").unwrap();
        assert_eq!(v.major, 1);
        assert_eq!(v.minor, 2);
        assert_eq!(v.patch, 3);
        assert_eq!(v.prerelease.as_deref(), Some("alpha.1"));
        assert_eq!(v.build.as_deref(), Some("build.123"));
        assert_eq!(to_string(&v), "1.2.3-alpha.1+build.123");
    }

    #[test]
    fn test_parse_invalid() {
        assert!(parse("01.2.3").is_err());
        assert!(parse("1.2").is_err());
        assert!(parse("1.2.3-01").is_err());
        assert!(parse("1.2.3-").is_err());
        assert!(parse("").is_err());
    }

    #[test]
    fn test_precedence() {
        let versions = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ];
        for i in 0..versions.len() - 1 {
            let v1 = parse(versions[i]).unwrap();
            let v2 = parse(versions[i + 1]).unwrap();
            assert_eq!(compare(&v1, &v2), Ordering::Less);
        }
    }

    #[test]
    fn test_bumps() {
        let v = parse("1.2.3-alpha+build").unwrap();
        assert_eq!(to_string(&bump_major(&v)), "2.0.0");
        assert_eq!(to_string(&bump_minor(&v)), "1.3.0");
        assert_eq!(to_string(&bump_patch(&v)), "1.2.4");
    }
}
