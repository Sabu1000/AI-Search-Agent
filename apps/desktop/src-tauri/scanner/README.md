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
time is included where available to mitigate inode reuse. Reliable rename
tracking on filesystems without birth time still needs a persistent local
manifest.

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
packaging, the native picker/removal UI, SQLite, watchers, binary formats,
credentials, and sync verification are not included.

From `apps/desktop/src-tauri`:

```sh
cargo fmt --all --check
cargo test --locked -p uas-desktop-scanner
```
