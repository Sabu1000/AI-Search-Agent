"""Bounded deterministic extraction of searchable Gmail message metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import getaddresses
from typing import Literal, cast

from uas_connector_sdk import DocumentPerson

from .email_parser import decode_mime_header

_MAX_PEOPLE = 50
_MAX_METADATA_ADDRESSES = 10
_MAX_REFERENCES = 20
_MAX_HEADER_VALUES = 20
_MAX_HEADER_VALUE = 4_000


@dataclass(frozen=True)
class ParsedGmailMetadata:
    subject: str
    sender_header: str
    to_header: str
    cc_header: str
    date_header: str
    authors: tuple[str, ...]
    people: tuple[DocumentPerson, ...]
    provider_metadata: dict[str, object]


def extract_gmail_metadata(
    payload: dict[str, object], *, sent_at: datetime, attachment_count: int
) -> ParsedGmailMetadata:
    """Extract allowlisted RFC headers, people, and message relationships."""

    headers = _headers(payload)
    subject = _first(headers, "subject") or "(no subject)"
    sender_header = _first(headers, "from")
    to_header = _joined(headers, "to")
    cc_header = _joined(headers, "cc")
    date_header = _first(headers, "date")

    sender_addresses = _addresses(headers.get("from", []))
    to_addresses = _addresses(headers.get("to", []))
    cc_addresses = _addresses(headers.get("cc", []))
    bcc_addresses = _addresses(headers.get("bcc", []))
    reply_to_addresses = _addresses(headers.get("reply-to", []))
    people, people_truncated = _people(
        sender_addresses,
        to_addresses,
        cc_addresses,
        bcc_addresses,
        reply_to_addresses,
    )
    authors = tuple(
        identity for name, address in sender_addresses if (identity := address or name)
    )[:_MAX_PEOPLE]

    metadata: dict[str, object] = {
        "attachment_count": attachment_count,
        "internal_date": sent_at.isoformat().replace("+00:00", "Z"),
    }
    sender = _address_metadata(sender_addresses)
    recipients = {
        key: values
        for key, values in (
            ("to", _address_metadata(to_addresses)),
            ("cc", _address_metadata(cc_addresses)),
            ("bcc", _address_metadata(bcc_addresses)),
        )
        if values
    }
    reply_to = _address_metadata(reply_to_addresses)
    if sender:
        metadata["sender"] = sender
    if recipients:
        metadata["recipients"] = recipients
    if reply_to:
        metadata["reply_to"] = reply_to
    if date_header:
        metadata["rfc_date"] = date_header
    for header_name, metadata_name in (
        ("message-id", "rfc_message_id"),
        ("in-reply-to", "in_reply_to"),
    ):
        value = _first(headers, header_name)
        if value:
            metadata[metadata_name] = value[:512]
    references = _references(headers.get("references", []))
    if references:
        metadata["references"] = references
    if people_truncated:
        metadata["people_truncated"] = True

    return ParsedGmailMetadata(
        subject=subject,
        sender_header=sender_header,
        to_header=to_header,
        cc_header=cc_header,
        date_header=date_header,
        authors=authors,
        people=people,
        provider_metadata=metadata,
    )


def _headers(payload: dict[str, object]) -> dict[str, list[str]]:
    values = payload.get("headers")
    if not isinstance(values, list):
        return {}
    result: dict[str, list[str]] = {}
    for header in values:
        if not isinstance(header, dict):
            continue
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            decoded = decode_mime_header(value)[:_MAX_HEADER_VALUE]
            if decoded:
                collected = result.setdefault(name.casefold(), [])
                if len(collected) < _MAX_HEADER_VALUES:
                    collected.append(decoded)
    return result


def _first(headers: dict[str, list[str]], name: str) -> str:
    return next(iter(headers.get(name, [])), "")


def _joined(headers: dict[str, list[str]], name: str) -> str:
    return ", ".join(headers.get(name, []))[:_MAX_HEADER_VALUE]


def _addresses(values: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw_name, raw_address in getaddresses(values):
        name = " ".join(raw_name.replace("\x00", "").split())[:200]
        address = raw_address.strip().casefold()[:320]
        if address and (
            "@" not in address or any(character.isspace() for character in address)
        ):
            address = ""
        if name or address:
            result.append((name, address))
    return result


def _address_metadata(values: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {
            **({"address": address} if address else {}),
            **({"display_name": name} if name else {}),
        }
        for name, address in values[:_MAX_METADATA_ADDRESSES]
    ]


def _people(
    *groups: list[tuple[str, str]],
) -> tuple[tuple[DocumentPerson, ...], bool]:
    relationships = ("sender", "recipient", "recipient", "recipient", "participant")
    people: list[DocumentPerson] = []
    identities: set[tuple[str, str, str]] = set()
    total_candidates = sum(len(group) for group in groups)
    for relationship, group in zip(relationships, groups, strict=True):
        for name, address in group:
            identity_kind = "email" if address else "display_name"
            identifier = address or name.casefold()
            key = (relationship, identity_kind, identifier)
            if not identifier or key in identities:
                continue
            identities.add(key)
            people.append(
                DocumentPerson(
                    relationship=cast(
                        "Literal['sender', 'recipient', 'participant']", relationship
                    ),
                    identity_kind=cast(
                        "Literal['email', 'display_name']", identity_kind
                    ),
                    normalized_identifier=identifier,
                    display_name=name or None,
                )
            )
            if len(people) == _MAX_PEOPLE:
                return tuple(people), total_candidates > len(people)
    return tuple(people), False


def _references(values: list[str]) -> list[str]:
    references: list[str] = []
    for value in values:
        for reference in value.split():
            bounded = reference[:512]
            if bounded and bounded not in references:
                references.append(bounded)
            if len(references) == _MAX_REFERENCES:
                return references
    return references
