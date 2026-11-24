use std::collections::HashSet;
use std::ffi::OsStr;
use std::io::{self, Read};
use std::os::unix::ffi::OsStrExt;
use std::path::{Component, Path};
use std::time::UNIX_EPOCH;

use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt, OpenOptionsSyncExt};
use cap_std::fs::{Dir, File, Metadata, MetadataExt, OpenOptions};
use cap_std::time::SystemTime;
use ignore::gitignore::{Gitignore, GitignoreBuilder};
use sha2::{Digest, Sha256};

use crate::{ManifestRecord, RootNamespace, ScanError, SkipCounts, Snapshot};

const MAX_FILE_BYTES: u64 = 100_000_000;
const MAX_IGNORE_BYTES: u64 = 16_384;
const MAX_IGNORE_RULES: usize = 256;
const MAX_PATH_BYTES: usize = 1024;
const READ_BUFFER_BYTES: usize = 16_384;
const IGNORED_DIRS: &[&str] = &[
    "node_modules",
    "build",
    "dist",
    "out",
    "target",
    "venv",
    "virtualenv",
    "vendor",
    "__pycache__",
];

pub(super) struct Limits {
    entries: usize,
    depth: usize,
    read_bytes: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            entries: 10_000,
            depth: 32,
            read_bytes: 512_000_000,
        }
    }
}

pub(super) struct Root {
    cap: Dir,
    namespace: RootNamespace,
    absolute_path_hash: [u8; 32],
    filesystem: Filesystem,
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct Filesystem {
    device: u64,
    #[cfg(target_os = "linux")]
    mount_id: u64,
}

impl Filesystem {
    fn for_handle(
        #[cfg(target_os = "linux")] handle: impl rustix::fd::AsFd,
        #[cfg(not(target_os = "linux"))] _handle: impl std::os::fd::AsFd,
        metadata: &Metadata,
    ) -> io::Result<Self> {
        #[cfg(target_os = "linux")]
        let mount_id = {
            use rustix::fs::{statx, AtFlags, StatxFlags};
            let fields = statx(handle, "", AtFlags::EMPTY_PATH, StatxFlags::MNT_ID)?;
            if fields.stx_mask & StatxFlags::MNT_ID.bits() == 0 {
                return Err(io::Error::from(io::ErrorKind::Unsupported));
            }
            fields.stx_mnt_id
        };
        Ok(Self {
            device: metadata.dev(),
            #[cfg(target_os = "linux")]
            mount_id,
        })
    }
}

impl Root {
    pub(super) fn open(path: &Path, namespace: RootNamespace) -> Result<Self, ScanError> {
        if !path.is_absolute()
            || path.parent().is_none()
            || namespace == [0; 32]
            || path
                .components()
                .any(|part| matches!(part, Component::ParentDir | Component::CurDir))
        {
            return Err(ScanError::InvalidSelection);
        }

        // Walk only the explicit root's components using directory capabilities.
        // Never follow a symlink even while establishing the starting capability.
        let mut cap = Dir::open_ambient_dir("/", cap_std::ambient_authority())
            .map_err(|_| ScanError::CannotOpenRoot)?;
        let mut has_component = false;
        for component in path.components() {
            match component {
                Component::RootDir => {}
                Component::Normal(name) => {
                    cap = cap
                        .open_dir_nofollow(name)
                        .map_err(|_| ScanError::CannotOpenRoot)?;
                    has_component = true;
                }
                _ => return Err(ScanError::InvalidSelection),
            }
        }
        if !has_component {
            return Err(ScanError::InvalidSelection);
        }
        let metadata = cap.dir_metadata().map_err(|_| ScanError::CannotOpenRoot)?;
        let filesystem =
            Filesystem::for_handle(&cap, &metadata).map_err(|_| ScanError::CannotOpenRoot)?;
        // Use the OS path bytes instead of a lossy Unicode conversion.
        let absolute_path_hash = digest_parts(
            b"uas.desktop.root-path.v1",
            &[&namespace, path.as_os_str().as_bytes()],
        );
        Ok(Self {
            cap,
            namespace,
            absolute_path_hash,
            filesystem,
        })
    }

    pub(super) fn scan(&self, limits: Limits) -> Result<Snapshot, ScanError> {
        let rules = self.ignore_rules()?;
        let mut scan = Scan {
            root: self,
            rules,
            limits,
            records: Vec::new(),
            visited: HashSet::new(),
            entries: 0,
            read_bytes: 0,
            authoritative: true,
            skipped: SkipCounts::default(),
        };
        let metadata = self
            .cap
            .dir_metadata()
            .map_err(|_| ScanError::CannotOpenRoot)?;
        scan.visited.insert((metadata.dev(), metadata.ino()));
        scan.walk(&self.cap, Path::new(""), 0)?;
        scan.records
            .sort_unstable_by(|a, b| a.relative_path.cmp(&b.relative_path));
        Ok(Snapshot {
            records: scan.records,
            authoritative: scan.authoritative,
            skipped: scan.skipped,
        })
    }

    fn ignore_rules(&self) -> Result<Gitignore, ScanError> {
        let mut builder = GitignoreBuilder::new("");
        let name = OsStr::new(".uaisignore");
        let before = match self.cap.symlink_metadata(name) {
            Ok(metadata) => Some(metadata),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(_) => return Err(ScanError::IgnoreRulesInvalid),
        };
        if let Some(before) = before {
            if !before.is_file() || before.nlink() != 1 || before.len() > MAX_IGNORE_BYTES {
                return Err(ScanError::IgnoreRulesInvalid);
            }
            let mut file = open_file(&self.cap, name).map_err(|_| ScanError::IgnoreRulesInvalid)?;
            let opened = file.metadata().map_err(|_| ScanError::IgnoreRulesInvalid)?;
            if !same_file(&before, &opened)
                || Filesystem::for_handle(&file, &opened).ok() != Some(self.filesystem)
            {
                return Err(ScanError::IgnoreRulesInvalid);
            }
            let mut rules = String::new();
            file.by_ref()
                .take(MAX_IGNORE_BYTES + 1)
                .read_to_string(&mut rules)
                .map_err(|_| ScanError::IgnoreRulesInvalid)?;
            let after = file.metadata().map_err(|_| ScanError::IgnoreRulesInvalid)?;
            if !same_file(&opened, &after)
                || rules.len() as u64 != after.len()
                || rules.contains('\0')
            {
                return Err(ScanError::IgnoreRulesInvalid);
            }
            for (index, line) in rules.lines().enumerate() {
                if index >= MAX_IGNORE_RULES {
                    return Err(ScanError::IgnoreRulesInvalid);
                }
                builder
                    .add_line(None, line)
                    .map_err(|_| ScanError::IgnoreRulesInvalid)?;
            }
        }
        builder.build().map_err(|_| ScanError::IgnoreRulesInvalid)
    }
}

struct Scan<'a> {
    root: &'a Root,
    rules: Gitignore,
    limits: Limits,
    records: Vec<ManifestRecord>,
    visited: HashSet<(u64, u64)>,
    entries: usize,
    read_bytes: u64,
    authoritative: bool,
    skipped: SkipCounts,
}

impl Scan<'_> {
    fn walk(&mut self, here: &Dir, parent: &Path, depth: usize) -> Result<(), ScanError> {
        if depth > self.limits.depth {
            return Err(ScanError::LimitExceeded);
        }
        let initial_metadata = match here.dir_metadata() {
            Ok(metadata) if metadata.nlink() > 0 => metadata,
            _ => {
                self.authoritative = false;
                return Ok(());
            }
        };
        let entries = match here.entries() {
            Ok(entries) => entries,
            Err(_) => {
                self.authoritative = false;
                return Ok(());
            }
        };
        for entry in entries {
            self.entries += 1;
            if self.entries > self.limits.entries {
                return Err(ScanError::LimitExceeded);
            }
            let entry = match entry {
                Ok(entry) => entry,
                Err(_) => {
                    self.authoritative = false;
                    continue;
                }
            };
            let name = entry.file_name();
            let Some(spelling) = name.to_str() else {
                self.skipped.excluded += 1;
                continue;
            };
            if spelling.starts_with('.') || !portable_name(spelling) {
                self.skipped.excluded += 1;
                continue;
            }
            let relative = parent.join(&name);
            let Some(logical_path) = relative.to_str() else {
                self.skipped.excluded += 1;
                continue;
            };
            if logical_path.len() > MAX_PATH_BYTES {
                self.skipped.excluded += 1;
                continue;
            }
            let before = match here.symlink_metadata(&name) {
                Ok(metadata) => metadata,
                Err(_) => {
                    self.authoritative = false;
                    continue;
                }
            };
            if self
                .rules
                .matched_path_or_any_parents(&relative, before.is_dir())
                .is_ignore()
            {
                self.skipped.excluded += 1;
                continue;
            }
            if before.dev() != self.root.filesystem.device {
                self.skipped.excluded += 1;
                self.authoritative = false;
                continue;
            }
            if before.is_dir() {
                if IGNORED_DIRS.contains(&spelling.to_ascii_lowercase().as_str()) {
                    self.skipped.excluded += 1;
                    continue;
                }
                let child = match here.open_dir_nofollow(&name) {
                    Ok(child) => child,
                    Err(_) => {
                        self.authoritative = false;
                        continue;
                    }
                };
                let child_metadata = match child.dir_metadata() {
                    Ok(metadata) => metadata,
                    Err(_) => {
                        self.authoritative = false;
                        continue;
                    }
                };
                if !same_identity(&before, &child_metadata) {
                    self.authoritative = false;
                    continue;
                }
                if Filesystem::for_handle(&child, &child_metadata).ok()
                    != Some(self.root.filesystem)
                {
                    self.skipped.excluded += 1;
                    self.authoritative = false;
                    continue;
                }
                if self
                    .visited
                    .insert((child_metadata.dev(), child_metadata.ino()))
                {
                    self.walk(&child, &relative, depth + 1)?;
                } else {
                    self.skipped.excluded += 1;
                    self.authoritative = false;
                }
            } else if !before.is_file() || before.nlink() != 1 {
                self.skipped.excluded += 1;
            } else if before.len() > MAX_FILE_BYTES {
                self.skipped.oversized += 1;
            } else if let Some(mime_type) = allowed_text_type(&relative) {
                self.read_file(here, &name, logical_path, &before, mime_type)?;
            } else {
                self.skipped.not_supported_text += 1;
            }
        }
        match here.dir_metadata() {
            Ok(metadata)
                if same_identity(&initial_metadata, &metadata)
                    && initial_metadata.nlink() == metadata.nlink()
                    && same_revision(&initial_metadata, &metadata) => {}
            _ => self.authoritative = false,
        }
        Ok(())
    }

    fn read_file(
        &mut self,
        here: &Dir,
        name: &OsStr,
        logical_path: &str,
        before: &Metadata,
        mime_type: &'static str,
    ) -> Result<(), ScanError> {
        let mut file = match open_file(here, name) {
            Ok(file) => file,
            Err(_) => {
                self.authoritative = false;
                return Ok(());
            }
        };
        let opened = match file.metadata() {
            Ok(metadata) => metadata,
            Err(_) => {
                self.authoritative = false;
                return Ok(());
            }
        };
        if !same_file(before, &opened)
            || Filesystem::for_handle(&file, &opened).ok() != Some(self.root.filesystem)
        {
            self.authoritative = false;
            return Ok(());
        }
        if self.read_bytes.saturating_add(opened.len()) > self.limits.read_bytes {
            return Err(ScanError::LimitExceeded);
        }
        let hashed = hash_text(
            &mut file,
            opened.len(),
            &mut self.read_bytes,
            self.limits.read_bytes,
        )?;
        let after = match file.metadata() {
            Ok(after) if same_file(&opened, &after) => after,
            _ => {
                self.authoritative = false;
                return Ok(());
            }
        };
        let content_sha256 = match hashed {
            FileHash::Digest(value) => value,
            FileHash::NotText => {
                self.skipped.not_supported_text += 1;
                return Ok(());
            }
            FileHash::Changed => {
                self.authoritative = false;
                return Ok(());
            }
        };
        let modified_at = match after.modified() {
            Ok(modified) => modified.into_std(),
            Err(_) => {
                self.authoritative = false;
                return Ok(());
            }
        };
        let device = after.dev().to_be_bytes();
        let inode = after.ino().to_be_bytes();
        let birth = after.created().ok().map(encode_time).unwrap_or([0; 13]);
        let absolute_path_sha256 = digest_parts(
            b"uas.desktop.file-path.v1",
            &[&self.root.absolute_path_hash, logical_path.as_bytes()],
        );
        let external_id = digest_parts(
            b"uas.desktop.file-identity.v1",
            &[&self.root.namespace, &device, &inode, &birth],
        );
        self.records.push(ManifestRecord {
            relative_path: logical_path.to_string(),
            absolute_path_sha256,
            external_id,
            size_bytes: after.len(),
            modified_at,
            content_sha256,
            mime_type,
        });
        Ok(())
    }
}

fn same_identity(left: &Metadata, right: &Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

fn same_file(left: &Metadata, right: &Metadata) -> bool {
    left.is_file()
        && right.is_file()
        && same_identity(left, right)
        && left.nlink() == 1
        && right.nlink() == 1
        && left.len() == right.len()
        && same_revision(left, right)
}

fn same_revision(left: &Metadata, right: &Metadata) -> bool {
    left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime() == right.ctime()
        && left.ctime_nsec() == right.ctime_nsec()
}

fn portable_name(name: &str) -> bool {
    !name.ends_with([' ', '.'])
        && !name
            .chars()
            .any(|c| c.is_control() || matches!(c, '<' | '>' | ':' | '"' | '\\' | '|' | '?' | '*'))
}

fn allowed_text_type(path: &Path) -> Option<&'static str> {
    match path.extension()?.to_str()?.to_ascii_lowercase().as_str() {
        "md" | "markdown" => Some("text/markdown"),
        "txt" => Some("text/plain"),
        "csv" => Some("text/csv"),
        _ => None,
    }
}

fn open_file(directory: &Dir, name: &OsStr) -> io::Result<File> {
    let mut options = OpenOptions::new();
    // Refuse racing symlink swaps and never block opening a swapped FIFO.
    options.read(true).follow(FollowSymlinks::No).nonblock(true);
    directory.open_with(name, &options)
}

enum FileHash {
    Digest([u8; 32]),
    NotText,
    Changed,
}

fn hash_text(
    mut file: impl Read,
    expected_bytes: u64,
    total_read: &mut u64,
    limit: u64,
) -> Result<FileHash, ScanError> {
    let mut hash = Sha256::new();
    let mut piece = [0; READ_BUFFER_BYTES + 3];
    let mut pending = 0;
    let mut file_bytes = 0;
    loop {
        let read = match file.read(&mut piece[pending..]) {
            Ok(0) => break,
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => return Ok(FileHash::Changed),
        };
        file_bytes += read as u64;
        *total_read += read as u64;
        if *total_read > limit {
            return Err(ScanError::LimitExceeded);
        }
        if file_bytes > expected_bytes {
            return Ok(FileHash::Changed);
        }
        hash.update(&piece[pending..pending + read]);
        let bytes = &piece[..pending + read];
        if bytes.contains(&0) {
            return Ok(FileHash::NotText);
        }
        match std::str::from_utf8(bytes) {
            Ok(_) => pending = 0,
            Err(error) if error.error_len().is_some() => return Ok(FileHash::NotText),
            Err(error) => {
                pending = bytes.len() - error.valid_up_to();
                piece.copy_within(error.valid_up_to()..error.valid_up_to() + pending, 0);
            }
        }
    }
    if file_bytes != expected_bytes {
        Ok(FileHash::Changed)
    } else if pending != 0 {
        Ok(FileHash::NotText)
    } else {
        Ok(FileHash::Digest(hash.finalize().into()))
    }
}

fn digest_parts(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(domain);
    for part in parts {
        hash.update((part.len() as u64).to_be_bytes());
        hash.update(part);
    }
    hash.finalize().into()
}

fn encode_time(time: SystemTime) -> [u8; 13] {
    let result = time.into_std().duration_since(UNIX_EPOCH);
    let (sign, duration) = match result {
        Ok(duration) => (1, duration),
        Err(error) => (2, error.duration()),
    };
    let mut bytes = [0_u8; 13];
    bytes[0] = sign;
    bytes[1..9].copy_from_slice(&duration.as_secs().to_be_bytes());
    bytes[9..13].copy_from_slice(&duration.subsec_nanos().to_be_bytes());
    bytes
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SelectedRoot;
    use std::fs;
    use std::os::unix::fs::{symlink, DirBuilderExt};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const ROOT_KEY: RootNamespace = [1; 32];

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "uas-desktop-scan-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
            Self(fs::canonicalize(path).unwrap())
        }

        fn write(&self, name: &str, body: &[u8]) {
            let destination = self.0.join(name);
            fs::create_dir_all(destination.parent().unwrap()).unwrap();
            fs::write(destination, body).unwrap();
        }

        fn root(&self) -> SelectedRoot {
            SelectedRoot::open_after_selection(&self.0, ROOT_KEY).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn scans_sorted_relative_text_only_and_matches_known_hash() {
        let fixture = Fixture::new();
        fixture.write("sub/hello.md", b"abc");
        fixture.write("root.CSV", b"one,two");
        fixture.write("image.bin", b"not selected");
        fixture.write("binary.txt", b"not\0text");
        fixture.write("broken.txt", b"\xff");
        fixture.write("node_modules/never.txt", b"ignored");
        fixture.write(".git/hidden.txt", b"ignored");
        fixture.write("invalid\\name.txt", b"ignored");
        fixture.write("sub/archive.zip", b"ignored");
        fs::File::create(fixture.0.join("oversized.txt"))
            .unwrap()
            .set_len(MAX_FILE_BYTES + 1)
            .unwrap();
        let snapshot = fixture.root().scan().unwrap();
        assert!(snapshot.authoritative);
        assert_eq!(
            snapshot.skipped,
            SkipCounts {
                excluded: 3,
                oversized: 1,
                not_supported_text: 4,
            }
        );
        let names: Vec<_> = snapshot
            .records
            .iter()
            .map(|record| record.relative_path.as_str())
            .collect();
        assert_eq!(names, ["root.CSV", "sub/hello.md"]);
        let hello = &snapshot.records[1];
        assert_eq!(
            hello.content_sha256,
            <[u8; 32]>::from(Sha256::digest(b"abc"))
        );
        assert_eq!(hello.size_bytes, 3);
        assert_eq!(hello.mime_type, "text/markdown");
    }

    #[test]
    fn explicit_root_ignores_gitignore_globs_and_never_overrides_safety() {
        let fixture = Fixture::new();
        fixture.write(
            ".uaisignore",
            b"# local rules\n*.txt\n!keep.txt\n/hidden-dir/\n!.git/secret.txt\n",
        );
        fixture.write("keep.txt", b"allowed");
        fixture.write("drop.txt", b"ignored");
        fixture.write("folder/drop.txt", b"ignored");
        fixture.write("folder/keep.txt", b"allowed");
        fixture.write("hidden-dir/note.md", b"ignored");
        fixture.write(".git/secret.txt", b"ignored");
        let snapshot = fixture.root().scan().unwrap();
        let names: Vec<_> = snapshot
            .records
            .iter()
            .map(|record| record.relative_path.as_str())
            .collect();
        assert_eq!(names, ["folder/keep.txt", "keep.txt"]);
    }

    #[test]
    fn selected_root_and_descendants_never_follow_links() {
        let fixture = Fixture::new();
        let outside = Fixture::new();
        outside.write("private.md", b"never read");
        symlink(outside.0.join("private.md"), fixture.0.join("link.md")).unwrap();
        symlink(&outside.0, fixture.0.join("link-dir")).unwrap();
        fs::hard_link(outside.0.join("private.md"), fixture.0.join("hard.md")).unwrap();
        fixture.write("keep.md", b"keep");
        let snapshot = fixture.root().scan().unwrap();
        assert_eq!(snapshot.records.len(), 1);
        assert_eq!(snapshot.records[0].relative_path, "keep.md");
        assert!(open_file(&fixture.root().directory.cap, OsStr::new("link.md")).is_err());
        assert_eq!(
            SelectedRoot::open_after_selection(&fixture.0.join("link-dir"), ROOT_KEY).err(),
            Some(ScanError::CannotOpenRoot),
        );
    }

    #[test]
    fn rejects_invalid_selection_and_untrusted_ignore_file() {
        assert_eq!(
            SelectedRoot::open_after_selection(Path::new("/"), ROOT_KEY).err(),
            Some(ScanError::InvalidSelection),
        );
        assert_eq!(
            SelectedRoot::open_after_selection(Path::new("../elsewhere"), ROOT_KEY).err(),
            Some(ScanError::InvalidSelection),
        );
        let fixture = Fixture::new();
        let outside = Fixture::new();
        outside.write("rules.txt", b"!anything");
        symlink(outside.0.join("rules.txt"), fixture.0.join(".uaisignore")).unwrap();
        assert_eq!(
            fixture.root().scan().err(),
            Some(ScanError::IgnoreRulesInvalid)
        );
        fs::remove_file(fixture.0.join(".uaisignore")).unwrap();
        fixture.write(".uaisignore", b"bad\0input");
        assert_eq!(
            fixture.root().scan().err(),
            Some(ScanError::IgnoreRulesInvalid)
        );
    }

    #[test]
    fn rename_keeps_identity_but_updates_path_and_namespaces_stay_distinct() {
        let fixture = Fixture::new();
        fixture.write("old.md", b"same contents");
        let root = fixture.root();
        let initial = root.scan().unwrap();
        fs::rename(fixture.0.join("old.md"), fixture.0.join("new.md")).unwrap();
        let after = root.scan().unwrap();
        assert_eq!(initial.records[0].external_id, after.records[0].external_id);
        assert_ne!(
            initial.records[0].absolute_path_sha256,
            after.records[0].absolute_path_sha256
        );
        let other = SelectedRoot::open_after_selection(&fixture.0, [2; 32])
            .unwrap()
            .scan()
            .unwrap();
        assert_ne!(after.records[0].external_id, other.records[0].external_id);
        assert_ne!(
            after.records[0].absolute_path_sha256,
            other.records[0].absolute_path_sha256
        );
    }

    #[test]
    fn limit_exhaustion_never_returns_a_partial_snapshot() {
        let fixture = Fixture::new();
        fixture.write("first.md", b"12345");
        fixture.write("nested/second.md", b"12345");
        for limits in [
            Limits {
                entries: 1,
                ..Limits::default()
            },
            Limits {
                depth: 0,
                ..Limits::default()
            },
            Limits {
                read_bytes: 8,
                ..Limits::default()
            },
        ] {
            assert_eq!(
                fixture.root().directory.scan(limits).err(),
                Some(ScanError::LimitExceeded)
            );
        }
    }

    #[test]
    fn hashes_utf8_split_on_chunk_boundary_and_detects_short_reads() {
        let mut input = vec![b'x'; READ_BUFFER_BYTES + 1];
        input.extend_from_slice("😀tail".as_bytes());
        let mut consumed = 0;
        let result =
            hash_text(input.as_slice(), input.len() as u64, &mut consumed, 100_000).unwrap();
        assert!(matches!(
            result,
            FileHash::Digest(digest) if digest == <[u8; 32]>::from(Sha256::digest(&input))
        ));
        assert_eq!(consumed, input.len() as u64);
        let result = hash_text(&b"a"[..], 2, &mut consumed, 100_000).unwrap();
        assert!(matches!(result, FileHash::Changed));
    }
}
