from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

from test_conversation_routes import HEADERS
from test_conversation_stream import ATTEMPT, BODY, ENDPOINT, fixture
from universal_ai_search.conversations.attempts import StoredStreamDelivery
from universal_ai_search.conversations.extracts import LiveExtract


def test_replay_only_renders_freshly_authorized_extracts() -> None:
    client, store, repository, _ = fixture()
    store.reserve.return_value = replace(ATTEMPT, replay=True)
    evidence = LiveExtract(
        source_id=uuid4(),
        chunk_id=uuid4(),
        version_id=uuid4(),
        chunk_hash=b"synthetic",
        score=0.9,
        provider="github",
        source_type="repository_file",
        title="Synthetic source",
        excerpt="A bound must be set. <script>",
        claim_id=uuid4(),
        citation_id=uuid4(),
    )
    revoked = False

    @asynccontextmanager
    async def delivery(**_: object):
        yield (
            StoredStreamDelivery("insufficient_evidence", ())
            if revoked
            else StoredStreamDelivery("complete", (evidence,))
        )

    store.delivery = delivery
    with client:
        allowed = client.post(ENDPOINT, headers=HEADERS, json=BODY)
        revoked = True
        unavailable = client.post(ENDPOINT, headers=HEADERS, json=BODY)
    assert allowed.status_code == unavailable.status_code == 200
    assert "event: claim.completed" in allowed.text
    assert "event: message.completed" in allowed.text
    assert str(evidence.citation_id) in allowed.text
    assert "event: message.insufficient" in unavailable.text
    assert "Synthetic source" not in unavailable.text
    assert evidence.excerpt not in unavailable.text
    repository.retrieve.assert_not_awaited()
    store.finish_with_pointers.assert_not_awaited()
