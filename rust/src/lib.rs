#[derive(Debug, PartialEq, Eq, Clone)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Option<String>,
    pub build: Option<String>,
}

impl Ord for Version {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.major.cmp(&other.major)
            .then(self.minor.cmp(&other.minor))
            .then(self.patch.cmp(&other.patch))
            .then(compare_prerelease(&self.prerelease, &other.prerelease))
    }
}

impl PartialOrd for Version {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
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
    let a_num = a.parse::<u64>();
    let b_num = b.parse::<u64>();
    match (a_num, b_num) {
        (Ok(a), Ok(b)) => a.cmp(&b),
        (Ok(_), Err(_)) => std::cmp::Ordering::Less,
        (Err(_), Ok(_)) => std::cmp::Ordering::Greater,
        (Err(_), Err(_)) => a.cmp(b),
    }
}

pub fn parse(s: &str) -> Result<Version, &'static str> {
    let parts: Vec<&str> = s.splitn(2, '+').collect();
    let main_and_prerelease = parts[0];
    let build = parts.get(1).map(|s| s.to_string());

    let parts: Vec<&str> = main_and_prerelease.splitn(2, '-').collect();
    let main = parts[0];
    let prerelease = parts.get(1).map(|s| s.to_string());

    let main_parts: Vec<&str> = main.split('.').collect();
    if main_parts.len() != 3 {
        return Err("invalid version");
    }

    let major = main_parts[0].parse().map_err(|_| "invalid major")?;
    let minor = main_parts[1].parse().map_err(|_| "invalid minor")?;
    let patch = main_parts[2].parse().map_err(|_| "invalid patch")?;

    Ok(Version {
        major,
        minor,
        patch,
        prerelease,
        build,
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
    a.cmp(b)
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
