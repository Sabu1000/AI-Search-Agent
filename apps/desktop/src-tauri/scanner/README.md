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

## Offline device signatures (v1)

This module provides Ed25519 possession proof and exact-request signing only.
It never reads the keychain, exports a private key, persists generated keys,
stores device credentials, consumes nonces, or makes network requests.
Production key import must come from the trusted desktop process's OS
keychain. Never invoke these entry points directly from the renderer.

A transcript consists of the ASCII fields below, in order, separated by one
LF byte. There is **no** trailing LF. UUIDs are nonzero, lowercase canonical
hyphenated strings. Sign the complete transcript bytes with regular Ed25519
(not a prehashed Ed25519 variant). Public keys and signatures are raw
32- and 64-byte values encoded as unpadded base64url.

Registration proof:

```text
uas-device-registration-v1
<challenge UUID>
<32 raw challenge bytes, unpadded base64url>
<32 raw public-key bytes, unpadded base64url>
```

The server must resolve the challenge UUID to a single-use, unexpired
challenge bound to the initiating authenticated user and workspace. The
proof is not itself a device credential.

Device requests:

```text
uas-device-request-v1
POST
<canonical /v1/devices/{device UUID}/{endpoint}>
<X-Device-Timestamp>
<X-Device-Nonce>
<Content-Digest>
<X-Request-ID>
```

- Only `heartbeat`, `manifests`, `uploads:sign`, and `changes` are allowed;
  no query, encoded path, alternate method, or trailing slash.
- `X-Device-Timestamp` is exactly `YYYY-MM-DDTHH:MM:SSZ`. Verification permits
  at most five minutes of skew in either direction.
- The signer generates a 16-byte CSPRNG nonce; verification accepts
  16–32 bytes encoded canonically in unpadded base64url.
- `Content-Digest` is `sha-256=:<standard padded base64>:`, following RFC
  9530, computed over the exact unmodified request bytes (maximum 2 MB).
- Put the signature in `X-Device-Signature`. The distinct
  `Authorization: Device <opaque credential>` is attached only by transport.
  The server must authenticate the credential, resolve the registered public
  key for that exact device, and atomically consume the nonce after verification.

The committed vector uses an intentionally public test seed and a synthetic
manifest. Both Rust and Python's `cryptography` verify the same keys,
transcripts and signatures. To repeat the cross-language check from
`apps/desktop/src-tauri` with the backend dependencies installed:

```sh
cargo test --locked -p uas-desktop-scanner
python scanner/fixtures/verify_ed25519_v1.py
```
