#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
project="uas-upload-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
export MINIO_ROOT_USER="uas-fixture-admin"
export MINIO_ROOT_PASSWORD
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
stage_id=$(openssl rand -hex 12)
stage_secret=$(openssl rand -hex 24)
export UAS_VERIFY_S3_ACCESS_KEY="$stage_id" UAS_VERIFY_S3_SECRET_KEY="$stage_secret"
export UAS_VERIFY_S3_URL=http://127.0.0.1:9000
export UAS_OBJECT_STORAGE_BUCKET=uas-upload-verification
compose=(docker compose --project-name "$project" --file "$root/compose.yaml")
cleanup() {
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf -- "$tmp"
}
trap cleanup EXIT
# Override published ports so fixture storage is only reachable on the VM.
cat >"$tmp/loopback.yaml" <<'YAML'
services:
  minio:
    ports: !override
      - "127.0.0.1:9000:9000"
      - "127.0.0.1:9001:9001"
YAML
compose+=(--file "$tmp/loopback.yaml")
"${compose[@]}" up -d --wait --wait-timeout 90 minio
cat >"$tmp/staging.json" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],
     "Resource":["arn:aws:s3:::uas-upload-verification/staging/*"]},
    {"Effect":"Deny","Action":["s3:DeleteObject","s3:DeleteObjectVersion"],
     "Resource":["arn:aws:s3:::uas-upload-verification/*"]}
  ]
}
JSON
# Pass fixture secrets only as environment variables to a pinned admin client.
docker run --rm --network "${project}_default" --entrypoint /bin/sh \
    -e MINIO_ROOT_USER -e MINIO_ROOT_PASSWORD \
    -e UAS_VERIFY_S3_ACCESS_KEY -e UAS_VERIFY_S3_SECRET_KEY -e UAS_OBJECT_STORAGE_BUCKET \
    -v "$tmp/staging.json:/fixture-policy.json:ro" \
    minio/mc:RELEASE.2025-08-13T08-35-41Z -ec '
    mc alias set setup http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc mb setup/"$UAS_OBJECT_STORAGE_BUCKET" >/dev/null
    mc admin user add setup "$UAS_VERIFY_S3_ACCESS_KEY" "$UAS_VERIFY_S3_SECRET_KEY" >/dev/null
    mc admin policy create setup verify-staging /fixture-policy.json >/dev/null
    mc admin policy attach setup verify-staging --user "$UAS_VERIFY_S3_ACCESS_KEY" >/dev/null
'
python3 -m venv "$tmp/venv"
"$tmp/venv/bin/python" -m pip --quiet install boto3==1.44.10
"$tmp/venv/bin/python" "$root/scripts/verify-upload-storage-contract.py"
