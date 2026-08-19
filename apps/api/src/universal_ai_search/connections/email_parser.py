"""Deterministic, inert extraction of searchable text from Gmail MIME payloads."""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from email.header import decode_header
from html.parser import HTMLParser
from typing import cast

from uas_connector_sdk.errors import MalformedItemError

_MAX_BODY_BYTES = 5_000_000
_CHARSET_PATTERN = re.compile(
    r"(?:^|;)\s*charset\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^;\s]+))",
    re.IGNORECASE,
)
_ORIGINAL_MESSAGE_PATTERN = re.compile(
    r"^-{2,}\s*(?:begin\s+)?original message\s*-{2,}$", re.IGNORECASE
)
_ON_WROTE_PATTERN = re.compile(r"^On\s+.+\bwrote:\s*$", re.IGNORECASE)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
)
_HIDDEN_TAGS = frozenset({"head", "noscript", "script", "style", "svg", "title"})
_QUOTE_CLASSES = frozenset({"gmail_extra", "gmail_quote"})
_SIGNATURE_CLASSES = frozenset({"gmail_signature"})


@dataclass(frozen=True)
class ParsedEmailBody:
    """Searchable message text plus auditable conservative-cleanup facts."""

    text: str
    body_format: str | None
    quoted_history_removed: bool
    signature_removed: bool
    skipped_attachment_count: int


@dataclass(frozen=True)
class _PartResult:
    text: str = ""
    body_format: str | None = None
    quoted_history_removed: bool = False
    signature_removed: bool = False
    skipped_attachment_count: int = 0


class _VisibleHTMLExtractor(HTMLParser):
    """Extract visible text while treating embedded email content as inert data."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipped_stack: list[tuple[str, str | None]] = []
        self._skip_depth = 0
        self.quoted_history_removed = False
        self.signature_removed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        attributes = {name.casefold(): value or "" for name, value in attrs}
        classes = frozenset(attributes.get("class", "").casefold().split())
        reason: str | None = None
        if normalized_tag in _HIDDEN_TAGS:
            reason = "hidden"
        elif normalized_tag == "blockquote" or classes & _QUOTE_CLASSES:
            reason = "quote"
            self.quoted_history_removed = True
        elif classes & _SIGNATURE_CLASSES:
            reason = "signature"
            self.signature_removed = True
        self._skipped_stack.append((normalized_tag, reason))
        if reason is not None:
            self._skip_depth += 1
        if normalized_tag in _BLOCK_TAGS and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if not self._skipped_stack:
            return
        stack_index = next(
            (
                index
                for index in range(len(self._skipped_stack) - 1, -1, -1)
                if self._skipped_stack[index][0] == normalized_tag
            ),
            None,
        )
        if stack_index is None:
            return
        removed = self._skipped_stack[stack_index:]
        del self._skipped_stack[stack_index:]
        self._skip_depth -= sum(reason is not None for _, reason in removed)
        if normalized_tag in _BLOCK_TAGS and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def decode_mime_header(value: str) -> str:
    """Decode RFC 2047 header words and prevent folded headers becoming new fields."""

    fragments: list[str] = []
    try:
        decoded = decode_header(value)
    except (LookupError, ValueError):
        decoded = [(value, None)]
    for fragment, charset in decoded:
        if isinstance(fragment, str):
            fragments.append(fragment)
            continue
        try:
            fragments.append(fragment.decode(charset or "ascii"))
        except (LookupError, UnicodeDecodeError):
            fragments.append(fragment.decode("utf-8", errors="replace"))
    return " ".join("".join(fragments).replace("\x00", "").split())


def parse_gmail_payload(payload: dict[str, object]) -> ParsedEmailBody:
    """Select and clean the searchable body from a Gmail full-message payload."""

    result = _parse_part(payload)
    return ParsedEmailBody(
        text=result.text,
        body_format=result.body_format,
        quoted_history_removed=result.quoted_history_removed,
        signature_removed=result.signature_removed,
        skipped_attachment_count=result.skipped_attachment_count,
    )


def _parse_part(part: dict[str, object]) -> _PartResult:
    mime_value = part.get("mimeType", "")
    if not isinstance(mime_value, str):
        raise MalformedItemError("Gmail MIME type is invalid")
    mime_type = mime_value.partition(";")[0].strip().casefold()
    children_value = part.get("parts")
    if children_value is not None and not isinstance(children_value, list):
        raise MalformedItemError("Gmail MIME parts are invalid")
    if isinstance(children_value, list) and not all(
        isinstance(child, dict) for child in children_value
    ):
        raise MalformedItemError("Gmail MIME parts are invalid")
    children = (
        [cast(dict[str, object], child) for child in children_value]
        if isinstance(children_value, list)
        else []
    )

    if _is_attachment(part) or mime_type == "message/rfc822":
        return _PartResult(skipped_attachment_count=1)
    if mime_type.startswith("multipart/") or children:
        parsed_children = [_parse_part(child) for child in children]
        if mime_type == "multipart/alternative":
            return _choose_alternative(parsed_children)
        return _combine_parts(parsed_children)
    if mime_type not in {"text/plain", "text/html"}:
        return _PartResult()

    body = part.get("body")
    if body is not None and not isinstance(body, dict):
        raise MalformedItemError("Gmail message body is invalid")
    data = body.get("data") if isinstance(body, dict) else None
    if data is None or data == "":
        return _PartResult()
    decoded = _decode_body(data, _charset(part))
    if mime_type == "text/html":
        return _extract_html(decoded)
    text, quote_removed, signature_removed = _clean_text(decoded)
    return _PartResult(
        text=text,
        body_format="plain" if text else None,
        quoted_history_removed=quote_removed,
        signature_removed=signature_removed,
    )


def _choose_alternative(results: list[_PartResult]) -> _PartResult:
    attachment_count = sum(result.skipped_attachment_count for result in results)
    chosen = next(
        (result for result in results if result.text and result.body_format == "plain"),
        next((result for result in results if result.text), _PartResult()),
    )
    return _PartResult(
        text=chosen.text,
        body_format=chosen.body_format,
        quoted_history_removed=chosen.quoted_history_removed,
        signature_removed=chosen.signature_removed,
        skipped_attachment_count=attachment_count,
    )


def _combine_parts(results: list[_PartResult]) -> _PartResult:
    texts: list[str] = []
    formats: set[str] = set()
    for result in results:
        if result.text and result.text not in texts:
            texts.append(result.text)
        if result.text and result.body_format is not None:
            formats.add(result.body_format)
    body_format: str | None = None
    if len(formats) == 1:
        body_format = next(iter(formats))
    elif formats:
        body_format = "mixed"
    return _PartResult(
        text="\n\n".join(texts),
        body_format=body_format,
        quoted_history_removed=any(result.quoted_history_removed for result in results),
        signature_removed=any(result.signature_removed for result in results),
        skipped_attachment_count=sum(
            result.skipped_attachment_count for result in results
        ),
    )


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


def _is_attachment(part: dict[str, object]) -> bool:
    filename = part.get("filename")
    if isinstance(filename, str) and filename.strip():
        return True
    disposition = _part_headers(part).get("content-disposition", "").casefold()
    if disposition.startswith("attachment"):
        return True
    body = part.get("body")
    return (
        isinstance(body, dict)
        and isinstance(body.get("attachmentId"), str)
        and not body.get("data")
    )


def _charset(part: dict[str, object]) -> str:
    content_type = _part_headers(part).get("content-type", "")
    match = _CHARSET_PATTERN.search(content_type)
    if match is None:
        return "utf-8"
    return next(value for value in match.groups() if value).strip()


def _decode_body(value: object, charset: str) -> str:
    if not isinstance(value, str):
        raise MalformedItemError("Gmail message body is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise MalformedItemError("Gmail message body is invalid") from error
    if len(decoded) > _MAX_BODY_BYTES:
        raise MalformedItemError("Gmail message body exceeds the extraction limit")
    try:
        return decoded.decode(charset)
    except LookupError:
        return decoded.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return decoded.decode(charset, errors="replace")


def _extract_html(value: str) -> _PartResult:
    extractor = _VisibleHTMLExtractor()
    try:
        extractor.feed(value)
        extractor.close()
    except (AssertionError, ValueError) as error:
        raise MalformedItemError("Gmail HTML body is invalid") from error
    text, plain_quote_removed, plain_signature_removed = _clean_text(
        "".join(extractor.parts)
    )
    return _PartResult(
        text=text,
        body_format="html" if text else None,
        quoted_history_removed=(
            extractor.quoted_history_removed or plain_quote_removed
        ),
        signature_removed=extractor.signature_removed or plain_signature_removed,
    )


def _clean_text(value: str) -> tuple[str, bool, bool]:
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    normalized = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"}
        or not unicodedata.category(character).startswith("C")
    )
    raw_lines = normalized.split("\n")
    signature_boundaries = {
        index for index, line in enumerate(raw_lines) if line == "-- "
    }
    lines = [" ".join(line.expandtabs().split()) for line in raw_lines]
    lines, quote_removed = _strip_quoted_suffix(lines)
    lines, signature_removed = _strip_signature(lines, signature_boundaries)
    compact: list[str] = []
    for line in lines:
        if line or not compact or compact[-1]:
            compact.append(line)
    return "\n".join(compact).strip(), quote_removed, signature_removed


def _strip_quoted_suffix(lines: list[str]) -> tuple[list[str], bool]:
    for index, line in enumerate(lines):
        if _ORIGINAL_MESSAGE_PATTERN.fullmatch(line):
            return lines[:index], True
        following_lines = [following for following in lines[index + 1 :] if following]
        if (
            _ON_WROTE_PATTERN.fullmatch(line)
            and following_lines
            and all(following.startswith(">") for following in following_lines)
        ):
            return lines[:index], True

    meaningful = [index for index, line in enumerate(lines) if line]
    if not meaningful or not lines[meaningful[-1]].startswith(">"):
        return lines, False
    first_quote = meaningful[-1]
    for index in reversed(meaningful[:-1]):
        if lines[index].startswith(">"):
            first_quote = index
            continue
        break
    return lines[:first_quote], True


def _strip_signature(
    lines: list[str], signature_boundaries: set[int]
) -> tuple[list[str], bool]:
    for index, line in enumerate(lines):
        if (
            index in signature_boundaries
            and line == "--"
            and any(following for following in lines[index + 1 :])
        ):
            return lines[:index], True
    return lines, False
