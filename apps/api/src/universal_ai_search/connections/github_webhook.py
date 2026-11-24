"""Authenticate GitHub App webhooks and emit content-free reconciliation hints.

Give this verifier the original, uncompressed request bytes and single-valued
headers; the HTTP boundary must cap bytes while reading the request. A hint is
not a grant, a job, a deletion, or proof of freshness. The ingress must resolve
the installation against current durable grants and claim each delivery before
queueing. Workers must independently recheck provider and workspace authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

MAX_GITHUB_WEBHOOK_BYTES = 2 * 1024 * 1024
MAX_INSTALLATION_REPOSITORIES = 500
_MAX_ID = 2**63 - 1
_SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}")
_DELIVERY = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_EVENT = re.compile(r"[a-z][a-z_]{0,63}")

_REPOSITORY_ACTIONS = frozenset(
    {
        "archived",
        "deleted",
        "edited",
        "privatized",
        "publicized",
        "renamed",
        "transferred",
        "unarchived",
    }
)
_REPOSITORY_SCOPE_ACTIONS = frozenset({"deleted", "transferred", "privatized"})
_REPOSITORY_CONTENT_FIELDS = frozenset(
    {"issue", "comment", "pull_request", "review", "ref", "ref_type"}
)
_ISSUE_ACTIONS = frozenset(
    {
        "closed",
        "deleted",
        "edited",
        "opened",
        "reopened",
        "transferred",
        "typed",
        "untyped",
    }
)
_COMMENT_ACTIONS = frozenset({"created", "deleted", "edited"})
_PULL_REQUEST_ACTIONS = frozenset(
    {
        "closed",
        "converted_to_draft",
        "edited",
        "opened",
        "ready_for_review",
        "reopened",
        "synchronize",
    }
)
_REVIEW_ACTIONS = frozenset({"dismissed", "edited", "submitted"})
_INSTALLATION_ACTIONS = frozenset(
    {"created", "deleted", "new_permissions_accepted", "suspend", "unsuspend"}
)

# Event/action names are deliberately limited to the facets the connector reads.
_CONTENT_EVENTS = MappingProxyType(
    {
        "issues": (_ISSUE_ACTIONS, "issue"),
        "issue_comment": (_COMMENT_ACTIONS, "issue"),
        "pull_request": (_PULL_REQUEST_ACTIONS, "pull_request"),
        "pull_request_review": (_REVIEW_ACTIONS, "pull_request"),
        "pull_request_review_comment": (_COMMENT_ACTIONS, "pull_request"),
        "repository": (_REPOSITORY_ACTIONS, None),
    }
)


class GitHubWebhookError(ValueError):
    """Invalid delivery; messages contain fixed codes only."""


class GitHubWebhookWork(StrEnum):
    REPOSITORY = "reconcile_repository"
    INSTALLATION = "reconcile_installation"
    REPOSITORY_SCOPE = "reconcile_repository_scope"


@dataclass(frozen=True, slots=True)
class GitHubWebhookHint:
    delivery_id: UUID
    payload_hash: bytes
    installation_id: int
    event_name: str
    action: str | None
    work: GitHubWebhookWork
    repository_id: int | None = None


def verify_github_webhook(
    *,
    raw_body: bytes,
    secret: bytes,
    signature: str,
    delivery_id: str,
    event_name: str,
) -> GitHubWebhookHint | None:
    """Return a verified hint, or None for an authenticated ignored delivery.

    The HMAC covers the body, not headers. Never use these headers as a grant
    or a freshness signal. GitHub does not provide a signed delivery timestamp.
    """
    if len(secret) < 32:
        raise GitHubWebhookError("GITHUB_WEBHOOK_NOT_CONFIGURED")
    if not 0 < len(raw_body) <= MAX_GITHUB_WEBHOOK_BYTES:
        raise GitHubWebhookError("GITHUB_WEBHOOK_BODY_SIZE_INVALID")
    if not _SIGNATURE.fullmatch(signature):
        raise GitHubWebhookError("GITHUB_WEBHOOK_SIGNATURE_INVALID")
    expected = hmac.digest(secret, raw_body, "sha256")
    received = bytes.fromhex(signature[7:])
    if not hmac.compare_digest(expected, received):
        raise GitHubWebhookError("GITHUB_WEBHOOK_SIGNATURE_INVALID")

    if not _DELIVERY.fullmatch(delivery_id):
        raise GitHubWebhookError("GITHUB_WEBHOOK_DELIVERY_ID_INVALID")
    delivery = UUID(delivery_id)
    if not delivery.int:
        raise GitHubWebhookError("GITHUB_WEBHOOK_DELIVERY_ID_INVALID")
    if not _EVENT.fullmatch(event_name):
        raise GitHubWebhookError("GITHUB_WEBHOOK_EVENT_INVALID")
    if event_name not in _CONTENT_EVENTS and event_name not in {
        "push",
        "installation",
        "installation_repositories",
        "installation_target",
    }:
        return None

    try:
        payload = _object(
            json.loads(
                raw_body.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        )
        action: str | None = None
        if event_name != "push":
            raw_action = payload.get("action")
            if not isinstance(raw_action, str) or not _EVENT.fullmatch(raw_action):
                raise ValueError
            action = raw_action

        if event_name == "installation":
            if action not in _INSTALLATION_ACTIONS:
                return None
            if "repository" in payload:
                raise ValueError
            kind = GitHubWebhookWork.INSTALLATION
        elif event_name == "installation_repositories":
            if action not in {"added", "removed"}:
                return None
            if "repository" in payload:
                raise ValueError
            added = _repository_ids(payload.get("repositories_added"))
            removed = _repository_ids(payload.get("repositories_removed"))
            if added & removed:
                raise ValueError
            kind = GitHubWebhookWork.INSTALLATION
        elif event_name == "installation_target":
            if action != "renamed":
                return None
            if "repository" in payload:
                raise ValueError
            kind = GitHubWebhookWork.INSTALLATION
        else:
            kind = GitHubWebhookWork.REPOSITORY
            content = _CONTENT_EVENTS.get(event_name)
            if content is not None:
                actions, object_field = content
                if action not in actions:
                    return None
                if event_name == "repository" and action in _REPOSITORY_SCOPE_ACTIONS:
                    # GitHub signs the body, not X-GitHub-Event. A re-labelled
                    # issue/delete body must not become an authorization fence.
                    if _REPOSITORY_CONTENT_FIELDS.intersection(payload):
                        raise ValueError
                    kind = GitHubWebhookWork.REPOSITORY_SCOPE
                if object_field is not None:
                    _object_id(payload.get(object_field))
                if event_name in {"issue_comment", "pull_request_review_comment"}:
                    _object_id(payload.get("comment"))
                if event_name == "pull_request_review":
                    _object_id(payload.get("review"))
            elif "action" in payload:
                # A push has no action, unlike the other supported events.
                raise ValueError

        installation = _object_id(payload.get("installation"))
        repo_id = (
            _object_id(payload.get("repository"))
            if kind is not GitHubWebhookWork.INSTALLATION
            else None
        )
    except (ValueError, RecursionError):
        raise GitHubWebhookError("GITHUB_WEBHOOK_PAYLOAD_INVALID") from None

    return GitHubWebhookHint(
        delivery_id=delivery,
        payload_hash=hashlib.sha256(raw_body).digest(),
        installation_id=installation,
        event_name=event_name,
        action=action,
        work=kind,
        repository_id=repo_id,
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def _object_id(value: object) -> int:
    number = _object(value).get("id")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or not 0 < number <= _MAX_ID
    ):
        raise ValueError
    return number


def _repository_ids(value: object) -> set[int]:
    if not isinstance(value, list) or len(value) > MAX_INSTALLATION_REPOSITORIES:
        raise ValueError
    result = {_object_id(item) for item in value}
    if len(result) != len(value):
        raise ValueError
    return result


def _unique_object(entries: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(entries)
    if len(result) != len(entries):
        raise ValueError
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError
