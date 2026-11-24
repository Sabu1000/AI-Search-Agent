#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
export MINIO_ROOT_USER="uas-fixture-admin"
export MINIO_ROOT_PASSWORD
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
export UAS_VERIFY_S3_ACCESS_KEY UAS_VERIFY_S3_SECRET_KEY
UAS_VERIFY_S3_ACCESS_KEY=$(openssl rand -hex 12)
UAS_VERIFY_S3_SECRET_KEY=$(openssl rand -hex 24)
export UAS_VERIFY_S3_READ_KEY UAS_VERIFY_S3_READ_SECRET
UAS_VERIFY_S3_READ_KEY=$(openssl rand -hex 12)
UAS_VERIFY_S3_READ_SECRET=$(openssl rand -hex 24)
export UAS_VERIFY_S3_URL=http://127.0.0.1:9000
export UAS_OBJECT_STORAGE_BUCKET=uas-upload-verification
pid=""
cleanup() {
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; wait "$pid" || true; fi
    rm -rf -- "$tmp"
}
trap cleanup EXIT
# The Docker Hub image is no longer available; use pinned official native binaries.
curl --fail --silent --show-error --location --retry 2 \
    https://github.com/minio/minio/releases/download/RELEASE.2025-09-07T16-13-09Z/minio.linux-amd64.RELEASE.2025-09-07T16-13-09Z -o "$tmp/minio"
curl --fail --silent --show-error --location --retry 2 \
    https://github.com/minio/mc/releases/download/RELEASE.2025-08-13T08-35-41Z/mc.linux-amd64.RELEASE.2025-08-13T08-35-41Z -o "$tmp/mc"
printf '%s  %s
%s  %s
' \
    7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f "$tmp/minio" \
    01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891 "$tmp/mc" | sha256sum --check
chmod u+x "$tmp/minio" "$tmp/mc"
export MC_CONFIG_DIR="$tmp/mc-cfg"
mkdir -p "$tmp/data"
"$tmp/minio" server --address 127.0.0.1:9000 --console-address 127.0.0.1:9001 "$tmp/data" >"$tmp/server.log" 2>&1 &
pid=$!
for ((i=1;i<=30;i++)); do
    if curl --fail --silent "$UAS_VERIFY_S3_URL/minio/health/live" >/dev/null; then break; fi
    if ! kill -0 "$pid" 2>/dev/null; then echo "Pinned native MinIO stopped during startup"; exit 1; fi
    sleep 1
done
curl --fail --silent "$UAS_VERIFY_S3_URL/minio/health/live" >/dev/null
echo "Pinned native MinIO is healthy."
cat >"$tmp/staging.json" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect":"Allow","Action":["s3:PutObject"],
     "Resource":["arn:aws:s3:::uas-upload-verification/staging/*"]},
    {"Effect":"Deny","Action":["s3:DeleteObject","s3:DeleteObjectVersion"],
     "Resource":["arn:aws:s3:::uas-upload-verification/*"]}
  ]
}
JSON
cat >"$tmp/reader.json" <<'JSON'
{
  "Version":"2012-10-17",
  "Statement":[
    {"Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion"],
     "Resource":["arn:aws:s3:::uas-upload-verification/staging/*"]}
  ]
}
JSON
mc="$tmp/mc"
"$mc" alias set setup "$UAS_VERIFY_S3_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
"$mc" mb setup/"$UAS_OBJECT_STORAGE_BUCKET" >/dev/null
"$mc" version enable setup/"$UAS_OBJECT_STORAGE_BUCKET" >/dev/null
"$mc" admin user add setup "$UAS_VERIFY_S3_ACCESS_KEY" "$UAS_VERIFY_S3_SECRET_KEY" >/dev/null 2>&1
"$mc" admin policy create setup verify-staging "$tmp/staging.json" >/dev/null
"$mc" admin policy attach setup verify-staging --user "$UAS_VERIFY_S3_ACCESS_KEY" >/dev/null 2>&1
"$mc" admin user add setup "$UAS_VERIFY_S3_READ_KEY" "$UAS_VERIFY_S3_READ_SECRET" >/dev/null 2>&1
"$mc" admin policy create setup verify-reader "$tmp/reader.json" >/dev/null
"$mc" admin policy attach setup verify-reader --user "$UAS_VERIFY_S3_READ_KEY" >/dev/null 2>&1
python3 -m venv "$tmp/venv"
"$tmp/venv/bin/python" -m pip --quiet install boto3==1.44.10
"$tmp/venv/bin/python" "$root/scripts/verify-upload-storage-contract.py"
