# Authentication and OAuth

## Application authentication
- Email/password and optional social login
- Email verification required before connector authorization
- Access token lifetime: 15 minutes
- Refresh token lifetime: 30 days, rotated on use
- Session revocation on password reset or suspicious activity

## Google connection flow
1. User selects Connect Google.
2. API creates signed state with user, workspace, nonce, and return URL.
3. Browser redirects to Google consent.
4. Callback validates state and exchanges code.
5. Credentials are encrypted and stored.
6. Connection record is created.
7. Initial sync is queued.

## Google scopes
Use the narrowest read-only scopes that support Gmail and Drive. Separate connector consent if broader combined scopes reduce approval likelihood or user trust.

## GitHub flow
Use a GitHub App installation rather than a classic OAuth app.
- User installs app on selected repositories.
- Webhook verifies signature.
- Installation ID is stored.
- Short-lived installation tokens are generated as needed.

## State and PKCE
- Use PKCE where supported
- State expires after 10 minutes
- State is single-use
- Callback origin is fixed and allowlisted

## Token storage
- Refresh tokens encrypted with per-record data keys
- Data keys encrypted by KMS
- Access tokens should be short-lived and preferably not persisted
- Never expose provider tokens to the browser or desktop app

## Revocation
On disconnect:
1. Revoke provider token when supported.
2. Delete encrypted credentials.
3. Disable sync.
4. Queue source deletion.
5. Record audit event.
