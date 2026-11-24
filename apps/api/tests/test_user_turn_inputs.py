from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from universal_ai_search.api.problems import ProblemError
from universal_ai_search.conversations.repository import ConversationScope
from universal_ai_search.conversations.user_turn import (
    MAX_USER_CONTENT_LENGTH,
    UserTurnInputError,
    UserTurnStore,
    _prepare,
)

SCOPE = ConversationScope(uuid4(), uuid4(), uuid4(), 1, 1)
CONVERSATION = uuid4()
CLIENT_MESSAGE = uuid4()


def prepare(
    *, content: str, key: str = "fixture-key-0001", client_id: UUID = CLIENT_MESSAGE
) -> tuple[UUID, bytes, bytes, int]:
    return _prepare(
        secret=b"t" * 32,
        scope=SCOPE,
        conversation_id=CONVERSATION,
        client_message_id=client_id,
        content=content,
        idempotency_key=key,
    )


@pytest.mark.parametrize("content", ["", "   ", "\x00", "\ud800", "x" * 10_001])
def test_rejects_invalid_prompt_without_echo(content: str) -> None:
    with pytest.raises(UserTurnInputError, match="^$"):
        prepare(content=content)


def test_requires_nonzero_client_id_and_valid_idempotency_key() -> None:
    with pytest.raises(UserTurnInputError):
        prepare(content="Good prompt", client_id=UUID(int=0))
    with pytest.raises(ProblemError) as invalid_key:
        prepare(content="Good prompt", key="bad")
    assert invalid_key.value.code == "IDEMPOTENCY_KEY_INVALID"
    with pytest.raises(ValueError):
        UserTurnStore(cast(AsyncEngine, None), idempotency_secret=b"short")


def test_idempotency_fingerprint_binds_content_and_turn() -> None:
    text = " x " + "x" * (MAX_USER_CONTENT_LENGTH - 3)
    msg, key_hash, request_hash, lock = prepare(content=text)
    assert len(key_hash) == len(request_hash) == 32
    assert prepare(content=text) == (msg, key_hash, request_hash, lock)
    assert prepare(content=text.strip())[:2] == (msg, key_hash)
    assert prepare(content=text.strip())[2] != request_hash
    assert prepare(content=text, client_id=uuid4())[0] != msg
    changed_key = prepare(content=text, key="fixture-key-0002")
    assert changed_key[0] == msg
    assert changed_key[1] != key_hash
    assert changed_key[2] == request_hash
