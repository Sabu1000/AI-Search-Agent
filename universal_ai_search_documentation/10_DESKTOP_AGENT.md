# Desktop Agent

## Technology
- Tauri
- Rust for filesystem operations
- React and TypeScript for UI
- SQLite for local manifest and queue
- OS keychain for device credentials

## Responsibilities
- Register device
- Let user select folders
- Scan files and compute hashes
- Extract or upload supported content
- Watch for creates, modifications, moves, and deletions
- Queue changes offline
- Retry safely
- Show sync progress and failures

## Permission model
The app only accesses folders explicitly selected by the user. It must show the selected roots and allow removal at any time.

## Ignore rules
Default exclusions:
- `.git`
- `node_modules`
- virtual environments
- build outputs
- dependency caches
- hidden system files
- files over configured size
- binary and unsupported extensions

Support `.uaisignore` with gitignore-style syntax.

## Manifest record
- absolute path hash
- relative path
- file size
- modification time
- content hash
- source external ID
- sync state
- last error

## Sync protocol
1. Send manifest delta.
2. Server returns requested uploads and deletions.
3. Desktop requests signed upload URLs.
4. Upload encrypted payload.
5. Submit completion record.
6. Server indexes and returns status.

## Security
- Device key stored in OS keychain
- Mutual device registration challenge
- Signed requests
- No provider OAuth credentials on desktop
- Automatic logout and remote device revocation

## Packaging
- Signed macOS build
- Notarized macOS release
- Signed Windows installer
- Auto-update with signed release manifests
