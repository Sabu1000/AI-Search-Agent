"""Bounded extraction of stable Gmail attachment documents."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from uas_connector_sdk import DocumentPerson, NormalizedDocument, Provider
from uas_connector_sdk.errors import MalformedItemError

from .email_parser import decode_mime_header, parse_gmail_payload

MAX_ATTACHMENT_BYTES = 5_000_000
MAX_ATTACHMENTS_PER_MESSAGE = 100
_TEXT_APPLICATION_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/toml",
        "application/xml",
        "application/x-httpd-php",
        "application/x-ndjson",
        "application/x-sh",
        "application/x-yaml",
        "application/yaml",
    }
)


@dataclass(frozen=True)
class GmailAttachmentPart:
    """The bounded fields needed to identify and normalize one MIME attachment."""

    part_id: str
    filename: str
    mime_type: str
    size: int
    attachment_id: str | None
    data: str | None
    content_type: str
    content_disposition: str
    content_id: str | None


def is_textual_attachment(mime_type: str) -> bool:
    normalized = mime_type.partition(";")[0].strip().casefold()
    return normalized.startswith("text/") or normalized in _TEXT_APPLICATION_TYPES


def find_gmail_attachments(
    payload: dict[str, object],
) -> tuple[GmailAttachmentPart, ...]:
    """Find filename/disposition attachments in deterministic MIME-tree order."""

    result: list[GmailAttachmentPart] = []
    _walk_parts(payload, result)
    if len(result) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise MalformedItemError("Gmail message has too many attachments")
    part_ids = [attachment.part_id for attachment in result]
    if len(set(part_ids)) != len(part_ids):
        raise MalformedItemError("Gmail attachment part IDs are not unique")
    return tuple(result)


def external_text_part_ids(payload: dict[str, object]) -> tuple[str, ...]:
    """Return external bodies worth fetching for email or attachment text."""

    result: list[str] = []
    _walk_external_text_parts(payload, result)
    if len(result) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise MalformedItemError("Gmail message has too many external text parts")
    return tuple(dict.fromkeys(result))


def apply_external_part_data(
    payload: dict[str, object], data_by_attachment_id: dict[str, str]
) -> None:
    """Hydrate a private message copy with separately retrieved Gmail part data."""

    body = payload.get("body")
    if isinstance(body, dict):
        attachment_id = body.get("attachmentId")
        if isinstance(attachment_id, str) and attachment_id in data_by_attachment_id:
            body["data"] = data_by_attachment_id[attachment_id]
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                apply_external_part_data(
                    cast(dict[str, object], part), data_by_attachment_id
                )


def normalize_gmail_attachments(
    *,
    message_id: str,
    thread_id: str,
    subject: str,
    sender: str,
    author: str,
    sent_at: datetime,
    payload: dict[str, object],
    people: tuple[DocumentPerson, ...] = (),
) -> tuple[NormalizedDocument, ...]:
    """Create one stable normalized source for every Gmail attachment."""

    documents: list[NormalizedDocument] = []
    for attachment in find_gmail_attachments(payload):
        external_id = f"{message_id}:attachment:{attachment.part_id}"
        if len(external_id) > 512:
            raise MalformedItemError("Gmail attachment identity is too long")
        filename = attachment.filename or f"attachment-{attachment.part_id}"
        extracted = _extract_attachment_text(attachment)
        if extracted:
            content = f"Filename: {filename}\nParent subject: {subject}\n\n{extracted}"
            normalized_mime_type = attachment.mime_type
            extraction_status = "extracted"
        else:
            extraction_status = (
                "too_large" if attachment.size > MAX_ATTACHMENT_BYTES else "unsupported"
            )
            content = (
                f"Attachment: {filename}\n"
                f"Parent subject: {subject}\n"
                f"Media type: {attachment.mime_type}\n"
                f"Content extraction: {extraction_status}"
            )
            normalized_mime_type = "text/plain"
        documents.append(
            NormalizedDocument(
                external_id=external_id,
                provider=Provider.GMAIL,
                source_type="attachment",
                title=filename[:2000],
                content=content,
                canonical_url=(f"https://mail.google.com/mail/u/0/#all/{message_id}"),
                mime_type=normalized_mime_type,
                authors=(author[:500],) if author else (),
                created_at=sent_at,
                people=people,
                provider_metadata={
                    **(
                        {"content_disposition": attachment.content_disposition}
                        if attachment.content_disposition
                        else {}
                    ),
                    **(
                        {"content_id": attachment.content_id}
                        if attachment.content_id
                        else {}
                    ),
                    "extraction_status": extraction_status,
                    "filename": filename,
                    "original_mime_type": attachment.mime_type,
                    "parent_message_id": message_id,
                    "part_id": attachment.part_id,
                    "size": attachment.size,
                    "thread_id": thread_id,
                    **({"sender": sender} if sender else {}),
                },
            )
        )
    return tuple(documents)


def _walk_parts(part: dict[str, object], result: list[GmailAttachmentPart]) -> None:
    headers = _part_headers(part)
    filename_value = part.get("filename", "")
    if not isinstance(filename_value, str):
        raise MalformedItemError("Gmail attachment filename is invalid")
    filename = decode_mime_header(filename_value)
    disposition = headers.get("content-disposition", "").casefold()
    is_attachment = bool(filename) or disposition.startswith("attachment")
    if is_attachment:
        part_id = part.get("partId")
        mime_value = part.get("mimeType")
        body = part.get("body")
        if (
            not isinstance(part_id, str)
            or not part_id
            or not isinstance(mime_value, str)
            or not mime_value
            or not isinstance(body, dict)
        ):
            raise MalformedItemError("Gmail attachment is missing required fields")
        mime_type = mime_value.partition(";")[0].strip().casefold()
        if not mime_type or len(mime_type) > 255:
            raise MalformedItemError("Gmail attachment MIME type is invalid")
        size = body.get("size", 0)
        attachment_id = body.get("attachmentId")
        data = body.get("data")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise MalformedItemError("Gmail attachment size is invalid")
        if attachment_id is not None and not isinstance(attachment_id, str):
            raise MalformedItemError("Gmail attachment ID is invalid")
        if data is not None and not isinstance(data, str):
            raise MalformedItemError("Gmail attachment data is invalid")
        result.append(
            GmailAttachmentPart(
                part_id=part_id,
                filename=filename,
                mime_type=mime_type,
                size=size,
                attachment_id=attachment_id,
                data=data,
                content_type=headers.get("content-type", ""),
                content_disposition=headers.get("content-disposition", "")[:500],
                content_id=(headers.get("content-id", "")[:512] or None),
            )
        )
    _walk_children(part, lambda child: _walk_parts(child, result))


def _walk_external_text_parts(part: dict[str, object], result: list[str]) -> None:
    mime_value = part.get("mimeType", "")
    filename = part.get("filename", "")
    body = part.get("body")
    if isinstance(mime_value, str) and isinstance(body, dict):
        mime_type = mime_value.partition(";")[0].strip().casefold()
        attachment_id = body.get("attachmentId")
        size = body.get("size", 0)
        if (
            isinstance(attachment_id, str)
            and attachment_id
            and not body.get("data")
            and isinstance(size, int)
            and not isinstance(size, bool)
            and 0 <= size <= MAX_ATTACHMENT_BYTES
            and (not filename or is_textual_attachment(mime_type))
        ):
            result.append(attachment_id)
    _walk_children(part, lambda child: _walk_external_text_parts(child, result))


def _walk_children(
    part: dict[str, object], visit: Callable[[dict[str, object]], None]
) -> None:
    parts = part.get("parts")
    if parts is None:
        return
    if not isinstance(parts, list) or not all(
        isinstance(child, dict) for child in parts
    ):
        raise MalformedItemError("Gmail MIME parts are invalid")
    for child in parts:
        visit(cast(dict[str, object], child))


def _part_headers(part: dict[str, object]) -> dict[str, str]:
    headers = part.get("headers")
    if not isinstance(headers, list):
        return {}
    result: dict[str, str] = {}
    for header in headers:
        if not isinstance(header, dict):
            continue
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result.setdefault(name.casefold(), value.strip())
    return result


def _extract_attachment_text(attachment: GmailAttachmentPart) -> str:
    if (
        not is_textual_attachment(attachment.mime_type)
        or attachment.size > MAX_ATTACHMENT_BYTES
        or not attachment.data
    ):
        return ""
    parser_mime_type = (
        attachment.mime_type
        if attachment.mime_type in {"text/html", "text/plain"}
        else "text/plain"
    )
    headers = (
        [{"name": "Content-Type", "value": attachment.content_type}]
        if attachment.content_type
        else []
    )
    parsed = parse_gmail_payload(
        {
            "body": {"data": attachment.data},
            "headers": headers,
            "mimeType": parser_mime_type,
        }
    )
    return parsed.text
