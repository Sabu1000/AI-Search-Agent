# Selected-root manifest scanner

This local Rust core is a partial desktop milestone. It has no Tauri command,
network access, uploader, or persisted root store. A future native folder
picker must pass only a user-approved, symlink-free absolute directory and a
cryptographically random 32-byte namespace that it persists per device/root.
Do not accept paths from a renderer or re-open a removed approval.

The scanner opens the selected root and its children through directory
capabilities, refuses symlinks and multiply linked files, and stays on the
same filesystem (including the mount ID on Linux). It outputs only relative
paths, namespaced path and file identifiers, byte sizes, timestamps, MIME
types, SHA-256, and aggregate skip counts. It does not return raw contents or
absolute paths. Identity survives a rename on the same filesystem; birth
time is included where available to mitigate inode reuse. When birth time is
unavailable, inode reuse can still make an identity ambiguous.

The initial formats are UTF-8 `.md`, `.markdown`, `.txt`, and `.csv` without
NUL bytes. Defaults always exclude dotfiles and known generated/dependency
directories. A root-only, bounded UTF-8 `.uaisignore` adds Git-style rules
without overriding safety exclusions. Scans are bounded to 10,000 entries,
32 directory levels, 512 MB total content read, and 100 MB per file. A rule
failure or exhausted budget returns no snapshot. A snapshot with interrupted
or unstable entries cannot be used to infer deletions. Even an authoritative
snapshot is not an atomic filesystem view; synchronization must re-open and
verify content via the retained capability and reconcile changes.

Unix scanning is implemented; Linux requires a kernel with `statx` mount ID
support. Other platforms, including Windows today, fail closed. macOS
packaging, the native picker/removal UI, watchers, binary formats, device
credentials, uploads, and sync verification are not included.

## Local SQLite baseline

The default `persistent-manifest` feature compares authoritative snapshots
with a per-selection baseline in a bounded 64 MiB SQLite database. A trusted
caller provides a dedicated connection to a private app-data file, outside any
selected root. Never hand this API a database chosen by the user or a renderer.

Planning is read-only. Changes are classified as additions, modifications,
renames, or removals by namespaced identity. A plan can commit the baseline
only after the caller confirms downstream acceptance and only while the
saved revision and database identity still match; rejected, partial,
cross-root, and stale input never advances it. No file contents or absolute
paths are persisted. The store has no pending upload queue and does not
generate, submit, or acknowledge network requests itself.

From `apps/desktop/src-tauri`:

```sh
cargo fmt --all --check
cargo test --locked -p uas-desktop-scanner
```
