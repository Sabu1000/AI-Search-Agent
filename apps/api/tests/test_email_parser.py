from __future__ import annotations

import base64

import pytest
from uas_connector_sdk.errors import MalformedItemError

from universal_ai_search.connections.email_parser import parse_gmail_payload
from universal_ai_search.connections.gmail import normalize_gmail_message


def _encoded(value: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(value.encode(encoding)).rstrip(b"=").decode()


def _text_part(
    value: str,
    *,
    mime_type: str = "text/plain",
    charset: str = "utf-8",
    filename: str = "",
) -> dict[str, object]:
    return {
        "body": {"data": _encoded(value, charset)},
        "filename": filename,
        "headers": [
            {"name": "Content-Type", "value": f"{mime_type}; charset={charset}"}
        ],
        "mimeType": mime_type,
    }


def test_parser_prefers_plain_text_in_nested_alternatives() -> None:
    payload: dict[str, object] = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    _text_part("Preferred plain body"),
                    _text_part("<p>Duplicate HTML body</p>", mime_type="text/html"),
                ],
            },
            _text_part("A second inline text section"),
        ],
    }

    parsed = parse_gmail_payload(payload)

    assert parsed.text == "Preferred plain body\n\nA second inline text section"
    assert parsed.body_format == "plain"
    assert not parsed.quoted_history_removed
    assert not parsed.signature_removed


def test_parser_uses_declared_charset_and_normalizes_unicode_and_controls() -> None:
    latin_1 = parse_gmail_payload(
        _text_part("Café\x00 costs £5\r\n\r\n\r\nToday", charset="iso-8859-1")
    )
    decomposed_unicode = parse_gmail_payload(_text_part("Cafe\u0301"))

    assert latin_1.text == "Café costs £5\n\nToday"
    assert decomposed_unicode.text == "Café"


def test_parser_removes_only_high_confidence_plain_reply_boundaries() -> None:
    parsed = parse_gmail_payload(
        _text_part(
            "The current answer is here.\n\n"
            "On Tue, Jan 2, Sender <sender@example.test> wrote:\n"
            "> An older answer\n> More old text"
        )
    )
    ambiguous = parse_gmail_payload(
        _text_part("Use > as a comparison.\n> keep this quote\nCurrent ending")
    )
    interrupted_history = parse_gmail_payload(
        _text_part("On Tue, Pat wrote:\n> old text\nNew text that must remain")
    )

    assert parsed.text == "The current answer is here."
    assert parsed.quoted_history_removed
    assert ambiguous.text == "Use > as a comparison.\n> keep this quote\nCurrent ending"
    assert not ambiguous.quoted_history_removed
    assert interrupted_history.text == (
        "On Tue, Pat wrote:\n> old text\nNew text that must remain"
    )
    assert not interrupted_history.quoted_history_removed


def test_parser_removes_standard_plain_signature_delimiter() -> None:
    parsed = parse_gmail_payload(
        _text_part("I approve the plan.\n\n-- \nAlex\nExample Company")
    )
    ambiguous_rule = parse_gmail_payload(_text_part("Keep this\n--\nand this"))

    assert parsed.text == "I approve the plan."
    assert parsed.signature_removed
    assert ambiguous_rule.text == "Keep this\n--\nand this"
    assert not ambiguous_rule.signature_removed


def test_html_fallback_keeps_layout_and_ignores_inert_or_repeated_content() -> None:
    html = """
    <html><head><title>Not searchable</title><style>.x { color: red }</style></head>
    <body><p>Hello <strong>team</strong>.</p><script>stealSecrets()</script>
    <div>Second line<br>after break</div>
    <div class="gmail_signature">Private signature</div>
    <blockquote>Earlier reply</blockquote></body></html>
    """

    parsed = parse_gmail_payload(_text_part(html, mime_type="text/html"))

    assert parsed.text == "Hello team.\n\nSecond line\nafter break"
    assert "stealSecrets" not in parsed.text
    assert "Earlier reply" not in parsed.text
    assert "Private signature" not in parsed.text
    assert parsed.body_format == "html"
    assert parsed.quoted_history_removed
    assert parsed.signature_removed


def test_parser_skips_text_attachments_until_attachment_extraction_step() -> None:
    payload: dict[str, object] = {
        "mimeType": "multipart/mixed",
        "parts": [
            _text_part("Message body"),
            _text_part("attachment secret", filename="notes.txt"),
            {
                "body": {"attachmentId": "attachment-2", "size": 120},
                "filename": "diagram.png",
                "mimeType": "image/png",
            },
        ],
    }

    parsed = parse_gmail_payload(payload)

    assert parsed.text == "Message body"
    assert "attachment secret" not in parsed.text
    assert parsed.skipped_attachment_count == 2


def test_normalization_decodes_headers_and_records_parser_decisions() -> None:
    message: dict[str, object] = {
        "historyId": "8",
        "id": "message-encoded",
        "internalDate": "1704067200000",
        "labelIds": ["INBOX"],
        "payload": {
            "body": {"data": _encoded("Current body\n-- \nSignature that is removed")},
            "headers": [
                {"name": "Subject", "value": "=?UTF-8?B?Q2Fmw6kgcGxhbg==?="},
                {
                    "name": "From",
                    "value": "=?UTF-8?Q?Jos=C3=A9?= <jose@example.test>",
                },
            ],
            "mimeType": "text/plain",
        },
        "threadId": "thread-encoded",
    }

    document = normalize_gmail_message(message)

    assert document.title == "Café plan"
    assert document.authors == ("jose@example.test",)
    assert document.content == (
        "Subject: Café plan\nFrom: José <jose@example.test>\n\nCurrent body"
    )
    assert document.provider_metadata["body_format"] == "plain"
    assert document.provider_metadata["signature_removed"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"mimeType": "text/plain", "body": {"data": "%%%"}},
        {"mimeType": "multipart/mixed", "parts": ["not-a-part"]},
        {"mimeType": 7},
    ],
)
def test_parser_rejects_malformed_provider_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(MalformedItemError):
        parse_gmail_payload(payload)
