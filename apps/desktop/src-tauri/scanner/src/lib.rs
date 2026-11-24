//! Local-only manifest scanner. Obtaining a selected root is privileged; this
//! crate has no Tauri commands, device registration, persistence, or network.
#![forbid(unsafe_code)]

use std::path::Path;
use std::time::SystemTime;

#[cfg(unix)]
mod unix;

/// A caller-generated, cryptographically random namespace, persisted with this
/// selection on this device. Never send this value or its raw path to a server.
pub type RootNamespace = [u8; 32];

/// A local, unpaged manifest record; convert timestamps and digest encodings
/// to the wire contract only in a later synchronization layer.
#[derive(Clone, PartialEq, Eq)]
pub struct ManifestRecord {
    pub relative_path: String,
    pub absolute_path_sha256: [u8; 32],
    pub external_id: [u8; 32],
    pub size_bytes: u64,
    pub modified_at: SystemTime,
    pub content_sha256: [u8; 32],
    pub mime_type: &'static str,
}

/// No source paths are attached to these errors. A failed scan is not a
/// manifest and must never be interpreted as a set of deletions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScanError {
    InvalidSelection,
    UnsupportedPlatform,
    CannotOpenRoot,
    IgnoreRulesInvalid,
    LimitExceeded,
}

/// A report is incomplete if an entry could not be checked reliably, e.g.
/// because it changed during the walk. Do not infer deletions from this
/// snapshot unless it is explicitly authoritative.
pub struct Snapshot {
    pub records: Vec<ManifestRecord>,
    pub authoritative: bool,
    pub skipped: SkipCounts,
}

/// Content-free counters for displaying exclusions without disclosing paths.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SkipCounts {
    pub excluded: usize,
    pub oversized: usize,
    pub not_supported_text: usize,
}

pub struct SelectedRoot {
    #[cfg(unix)]
    directory: unix::Root,
}

impl SelectedRoot {
    /// Call only after the native picker has obtained the user's explicit
    /// approval of this absolute directory; do not expose a path-taking
    /// renderer command. The open directory capability stays with this object.
    pub fn open_after_selection(
        absolute_path: &Path,
        namespace: RootNamespace,
    ) -> Result<Self, ScanError> {
        #[cfg(unix)]
        {
            Ok(Self {
                directory: unix::Root::open(absolute_path, namespace)?,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = (absolute_path, namespace);
            Err(ScanError::UnsupportedPlatform)
        }
    }

    pub fn scan(&self) -> Result<Snapshot, ScanError> {
        #[cfg(unix)]
        {
            self.directory.scan(unix::Limits::default())
        }
        #[cfg(not(unix))]
        {
            Err(ScanError::UnsupportedPlatform)
        }
    }
}
