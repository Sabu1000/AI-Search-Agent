"""Confirm the pinned object store enforces the synthetic upload contract."""

import base64
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

BUCKET = os.environ["UAS_OBJECT_STORAGE_BUCKET"]
KEY = "staging/generated-fixture.txt"
DATA = b"synthetic upload fixture"
CHECKSUM = base64.b64encode(hashlib.sha256(DATA).digest()).decode("ascii")
HEADERS = {
    "Content-Length": str(len(DATA)),
    "Content-Type": "text/plain",
    "If-None-Match": "*",
    "X-Amz-Checksum-Sha256": CHECKSUM,
}


def put(url: str, data: bytes, headers: dict[str, str]) -> int:
    request = Request(url, method="PUT", data=data, headers=headers)
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (TimeoutError, URLError):
        raise AssertionError("local storage transport failed") from None


def make_client(key_variable: str, secret_variable: str):
    return boto3.client(
        "s3",
        endpoint_url=os.environ["UAS_VERIFY_S3_URL"],
        aws_access_key_id=os.environ[key_variable],
        aws_secret_access_key=os.environ[secret_variable],
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )


def verify() -> None:
    client = make_client("UAS_VERIFY_S3_ACCESS_KEY", "UAS_VERIFY_S3_SECRET_KEY")
    reader = make_client("UAS_VERIFY_S3_READ_KEY", "UAS_VERIFY_S3_READ_SECRET")
    url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET,
            "Key": KEY,
            "ContentLength": len(DATA),
            "ContentType": "text/plain",
            "IfNoneMatch": "*",
            "ChecksumSHA256": CHECKSUM,
        },
        ExpiresIn=120,
    )
    assert put(url, DATA, {**HEADERS, "Content-Type": "text/html"}) in {400, 403}
    assert put(url, b"x" * len(DATA), HEADERS) in {400, 403}
    assert put(url, DATA[:-1], {**HEADERS, "Content-Length": str(len(DATA) - 1)}) in {
        400,
        403,
    }
    assert put(
        url, DATA, {k: v for k, v in HEADERS.items() if k != "If-None-Match"}
    ) in {400, 403}
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = [workers.submit(put, url, DATA, HEADERS) for _ in range(2)]
        codes = sorted(future.result() for future in outcomes)
    assert codes[0] == 200 and codes[1] in {409, 412}
    assert put(url, DATA, HEADERS) in {409, 412}
    try:
        client.delete_object(Bucket=BUCKET, Key=KEY)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 403:
            raise AssertionError("unexpected delete denial response") from None
    else:
        raise AssertionError("staging credential unexpectedly allowed delete")
    try:
        client.head_object(Bucket=BUCKET, Key=KEY)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 403:
            raise AssertionError("unexpected signer denial response") from None
    else:
        raise AssertionError("signing identity unexpectedly allowed object read")
    verified = reader.head_object(Bucket=BUCKET, Key=KEY, ChecksumMode="ENABLED")
    version = verified.get("VersionId")
    assert version and version != "null"
    pinned = reader.head_object(
        Bucket=BUCKET, Key=KEY, VersionId=version, ChecksumMode="ENABLED"
    )
    assert pinned["VersionId"] == version
    assert pinned.get("ChecksumSHA256") == verified.get("ChecksumSHA256") == CHECKSUM
    assert verified["ContentLength"] == len(DATA)
    assert verified["ContentType"] == "text/plain"
    with reader.get_object(Bucket=BUCKET, Key=KEY, VersionId=version)["Body"] as body:
        assert body.read(len(DATA) + 1) == DATA


if __name__ == "__main__":
    try:
        verify()
    except (AssertionError, BotoCoreError, ClientError) as error:
        # Never put signed URLs, secret values, or raw SDK errors in public logs.
        line = error.__traceback__
        while line is not None and line.tb_next is not None:
            line = line.tb_next
        print(f"Storage verification failed at line {line.tb_lineno if line else 0}.")
        raise SystemExit(1) from None
    print(
        "Verified signed type, length, checksum, write-once upload and delete denial."
    )
