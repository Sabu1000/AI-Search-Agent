#!/bin/sh

set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
security="$repository_root/universal_ai_search_documentation/08_SECURITY_AND_PRIVACY.md"
authentication="$repository_root/universal_ai_search_documentation/09_AUTH_AND_OAUTH.md"
failures=0

for file in "$security" "$authentication"
do
  if [ ! -f "$file" ]; then
    printf 'ERROR: required security specification is missing: %s\n' "$file" >&2
    failures=$((failures + 1))
  fi
done

if [ "$failures" -ne 0 ]; then
  exit 1
fi

for heading in \
  '## Goals and ownership' \
  '## Security boundaries and threat model' \
  '## Data classification and minimization' \
  '## Tenant authorization' \
  '## Cryptography and secret management' \
  '## Browser, API, and content security' \
  '## Input, file, network, and provider safety' \
  '## Prompt-injection and model boundary' \
  '## Logging, auditing, and incident evidence' \
  '## Abuse prevention and operational controls' \
  '## Privacy choices and consent' \
  '## Retention, export, deletion, and backups' \
  '## Security verification and release gates' \
  '## Requirement coverage' \
  '## Stage 08 completion criteria'
do
  if ! grep -Fqx "$heading" "$security"; then
    printf 'ERROR: security specification is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for control in \
  'Argon2id' \
  'envelope' \
  'transaction-local' \
  'Content Security Policy' \
  'Cache-Control: private, no-store' \
  'parameterized' \
  'SSRF' \
  'prompt injection' \
  'Rate limits' \
  '24-hour' \
  'restore never' \
  'container-image checks'
do
  if ! grep -Fqi "$control" "$security"; then
    printf 'ERROR: security specification is missing control: %s\n' "$control" >&2
    failures=$((failures + 1))
  fi
done

for forbidden_log_value in \
  'passwords' \
  'OAuth' \
  'authorization headers' \
  'full request/response bodies' \
  'private prompts'
do
  if ! grep -Fqi "$forbidden_log_value" "$security"; then
    printf 'ERROR: security logging policy is missing sensitive class: %s\n' "$forbidden_log_value" >&2
    failures=$((failures + 1))
  fi
done

for heading in \
  '## Goals and ownership' \
  '## Identity and account state' \
  '## Password registration and storage' \
  '## Email verification and password reset' \
  '## Login and session issuance' \
  '## Browser and native credential transport' \
  '## Refresh rotation and replay response' \
  '## Reauthentication and sensitive actions' \
  '## Workspace authorization' \
  '## OAuth transaction contract' \
  '## Google connection flow and scopes' \
  '## GitHub App installation flow' \
  '## Provider credential envelope' \
  '## Disconnect, revocation, and compromise' \
  '## Audit, redaction, and failure behavior' \
  '## Authentication and OAuth test matrix' \
  '## Requirement coverage' \
  '## Stage 09 completion criteria'
do
  if ! grep -Fqx "$heading" "$authentication"; then
    printf 'ERROR: authentication specification is missing section: %s\n' "$heading" >&2
    failures=$((failures + 1))
  fi
done

for auth_rule in \
  '12–128' \
  'Argon2id' \
  'at most 15' \
  '30 days' \
  'seven-day idle limit' \
  'revoke the entire family' \
  '__Host-uas_access' \
  '__Host-uas_refresh' \
  '__Host-uas_csrf' \
  'X-CSRF-Token' \
  'Authorization: Bearer' \
  'five minutes' \
  'X-Workspace-ID' \
  'PKCE' \
  'S256' \
  '10 minutes' \
  'GitHub App' \
  'AES-256-GCM' \
  'authenticated additional data' \
  'single-flight' \
  'B4'
do
  if ! grep -Fq "$auth_rule" "$authentication"; then
    printf 'ERROR: authentication specification is missing contract: %s\n' "$auth_rule" >&2
    failures=$((failures + 1))
  fi
done

for requirement in AUTH-001 GOOGLE-001 GITHUB-001 SYNC-001 CONNECTION-001 ACCOUNT-001 SAFETY-001
do
  if ! grep -Fq "\`$requirement\`" "$authentication"; then
    printf 'ERROR: authentication specification does not map requirement: %s\n' "$requirement" >&2
    failures=$((failures + 1))
  fi
done

for requirement in AUTH-001 DESKTOP-001 GOOGLE-001 GITHUB-001 SYNC-001 SEARCH-001 ANSWER-001 CITATION-001 CONNECTION-001 ACCOUNT-001 SAFETY-001
do
  if ! grep -Fq "\`$requirement\`" "$security"; then
    printf 'ERROR: security specification does not map requirement: %s\n' "$requirement" >&2
    failures=$((failures + 1))
  fi
done

if grep -Eqi '\b(TODO|TBD|FIXME)\b' "$security" "$authentication"; then
  printf '%s\n' 'ERROR: security/auth specifications contain unresolved placeholders.' >&2
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  printf 'Security/auth validation failed with %s error(s).\n' "$failures" >&2
  exit 1
fi

printf '%s\n' 'Security and authentication specifications validated.'
