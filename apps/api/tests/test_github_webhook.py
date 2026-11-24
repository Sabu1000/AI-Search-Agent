from __future__ import annotations

import hashlib
import hmac
import json
from uuid import UUID

import pytest

from universal_ai_search.connections import github_webhook
from universal_ai_search.connections.github_webhook import (
    MAX_GITHUB_WEBHOOK_BYTES,
    MAX_INSTALLATION_REPOSITORIES,
    GitHubWebhookError,
    GitHubWebhookHint,
    GitHubWebhookWork,
    verify_github_webhook,
)

SECRET = b"fixture-secret-not-a-real-github-key"
DELIVERY = "ABCD1234-0000-4000-8000-001234567890"
BASE: dict[str, object] = {
    "installation": {"id": 123},
    "repository": {"id": 456},
    "ref": "refs/heads/main",
    "before": "a" * 40,
    "after": "b" * 40,
    "private_text": "not returned",
}


def body(payload: object = BASE) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, raw, "sha256").hexdigest()


def verify(
    raw: bytes,
    *,
    event: str = "push",
    delivery: str = DELIVERY,
    signature: str | None = None,
    secret: bytes = SECRET,
) -> GitHubWebhookHint | None:
    return verify_github_webhook(
        raw_body=raw,
        secret=secret,
        signature=signature if signature is not None else sign(raw),
        delivery_id=delivery,
        event_name=event,
    )


def test_verifies_exact_bytes_and_returns_only_normalized_identifiers() -> None:
    raw = body({**BASE, "private_text": "日本語"})
    hint = verify(raw)
    assert hint == GitHubWebhookHint(
        delivery_id=UUID(DELIVERY),
        payload_hash=hashlib.sha256(raw).digest(),
        installation_id=123,
        event_name="push",
        action=None,
        work=GitHubWebhookWork.REPOSITORY,
        repository_id=456,
    )
    assert "日本語" not in repr(hint)
    with pytest.raises(GitHubWebhookError, match="SIGNATURE_INVALID"):
        verify(raw + b" ", signature=sign(raw))


@pytest.mark.parametrize(
    "signature",
    [
        "",
        "sha1=" + "0" * 40,
        "sha256=" + "0" * 64,
        "sha256=" + "g" * 64,
        "sha256=" + "0" * 65,
        "sha256=" + "0" * 64 + "\n",
    ],
)
def test_invalid_signature_is_rejected_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch, signature: str
) -> None:
    def must_not_parse(*_args: object, **_kwargs: object) -> object:
        pytest.fail("JSON parsing before signature verification")

    monkeypatch.setattr(github_webhook.json, "loads", must_not_parse)
    with pytest.raises(GitHubWebhookError, match="SIGNATURE_INVALID"):
        verify(b"not valid JSON", signature=signature)


@pytest.mark.parametrize("raw", [b"", b"x" * (MAX_GITHUB_WEBHOOK_BYTES + 1)])
def test_body_is_bounded_even_with_valid_signature(raw: bytes) -> None:
    with pytest.raises(GitHubWebhookError, match="BODY_SIZE_INVALID"):
        verify(raw)


def test_refuses_disabled_or_weak_secret() -> None:
    with pytest.raises(GitHubWebhookError, match="NOT_CONFIGURED"):
        verify(body(), secret=b"too short")


@pytest.mark.parametrize(
    "delivery",
    ["", "{" + DELIVERY + "}", "0" * 36, str(UUID(int=0)), DELIVERY + ", " + DELIVERY],
)
def test_rejects_unusable_delivery_ids(delivery: str) -> None:
    with pytest.raises(GitHubWebhookError, match="DELIVERY_ID_INVALID"):
        verify(body(), delivery=delivery)


def test_ignores_authenticated_unknown_events_but_rejects_bad_header() -> None:
    assert verify(b"{}", event="ping") is None
    assert verify(b"{}", event="workflow_run") is None
    with pytest.raises(GitHubWebhookError, match="EVENT_INVALID"):
        verify(body(), event="push, issues")


@pytest.mark.parametrize(
    ("event", "action", "required"),
    [
        ("issues", "edited", {"issue": {"id": 4}}),
        ("issue_comment", "deleted", {"issue": {"id": 4}, "comment": {"id": 7}}),
        ("pull_request", "synchronize", {"pull_request": {"id": 4}}),
        (
            "pull_request_review",
            "submitted",
            {"pull_request": {"id": 4}, "review": {"id": 7}},
        ),
        (
            "pull_request_review_comment",
            "created",
            {"pull_request": {"id": 4}, "comment": {"id": 7}},
        ),
        ("repository", "deleted", {}),
    ],
)
def test_supported_content_events(
    event: str, action: str, required: dict[str, object]
) -> None:
    hint = verify(body({**BASE, **required, "action": action}), event=event)
    assert hint is not None
    assert hint.repository_id == 456
    assert hint.action == action
    assert hint.work is GitHubWebhookWork.REPOSITORY


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("installation", {"action": "suspend"}),
        ("installation_target", {"action": "renamed"}),
        (
            "installation_repositories",
            {
                "action": "removed",
                "repositories_added": [],
                "repositories_removed": [{"id": 456}],
            },
        ),
        (
            "installation_repositories",
            {"action": "added", "repositories_added": [], "repositories_removed": []},
        ),
    ],
)
def test_installation_hints_never_promote_payload_repositories(
    event: str, payload: dict[str, object]
) -> None:
    hint = verify(body({**payload, "installation": {"id": 123}}), event=event)
    assert hint is not None
    assert hint.work is GitHubWebhookWork.INSTALLATION
    assert hint.repository_id is None


@pytest.mark.parametrize(
    ("event", "action"),
    [
        ("installation", "unrecognized"),
        ("installation_repositories", "unrecognized"),
        ("installation_target", "unrecognized"),
        ("issues", "assigned"),
    ],
)
def test_ignores_actions_not_needed_for_current_connector(
    event: str, action: str
) -> None:
    assert verify(body({**BASE, "action": action}), event=event) is None


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("push", {**BASE, "action": "created"}),
        ("push", {**BASE, "repository": {"id": True}}),
        ("push", {**BASE, "installation": {"id": 2**63}}),
        ("push", {**BASE, "installation": None}),
        ("issues", {**BASE, "action": False}),
        ("issues", {**BASE, "action": "opened"}),
        ("issue_comment", {**BASE, "action": "deleted", "issue": {"id": 3}}),
        ("pull_request_review", {**BASE, "action": "submitted"}),
        (
            "installation_repositories",
            {
                **BASE,
                "action": "added",
                "repositories_added": [{"id": 123}]
                * (MAX_INSTALLATION_REPOSITORIES + 1),
                "repositories_removed": [],
            },
        ),
        (
            "installation_repositories",
            {
                **BASE,
                "action": "added",
                "repositories_added": [{"id": 456}, {"id": 456}],
                "repositories_removed": [],
            },
        ),
        (
            "installation_repositories",
            {
                **BASE,
                "action": "removed",
                "repositories_added": [{"id": 456}],
                "repositories_removed": [{"id": 456}],
            },
        ),
    ],
)
def test_rejects_malformed_authoritative_fields(event: str, payload: object) -> None:
    with pytest.raises(GitHubWebhookError, match="PAYLOAD_INVALID"):
        verify(body(payload), event=event)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{} nope",
        b"[]",
        b'{"repository":{"id":456},"installation":{"id":123,"id":1}}',
        b'{"repository":{"id":456},"installation":{"id":123},"ignored":NaN}',
        b"[" * 1500 + b"]" * 1500,
    ],
)
def test_rejects_duplicate_keys_invalid_utf8_and_nonstandard_json(raw: bytes) -> None:
    with pytest.raises(GitHubWebhookError, match="PAYLOAD_INVALID"):
        verify(raw)
