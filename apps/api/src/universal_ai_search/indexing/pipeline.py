"""Deterministic normalization, chunking, deduplication, and local embeddings."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID, uuid5

PARSER_VERSION = "normalized-text-v1"
CHUNKER_VERSION = "structure-words-v1"
EMBEDDING_PROVIDER = "local"
EMBEDDING_MODEL = "deterministic-sha256-v1"
EMBEDDING_DIMENSIONS = 1536
MAX_CHUNKS = 10_000
_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SPACE_PATTERN = re.compile(r"[^\S\n]+")
_BLANK_PATTERN = re.compile(r"\n{3,}")
_NORMALIZED_TEXT_MEDIA_TYPES = frozenset(
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
        "text/markdown",
        "text/plain",
    }
)


class IndexingError(Exception):
    """A safe indexing failure with a stable public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PendingDocument:
    workspace_id: UUID
    source_id: UUID
    document_version_id: UUID
    title: str
    content: str
    mime_type: str
    content_hash: bytes
    permissions_hash: bytes
    embedding_profile_id: int


@dataclass(frozen=True)
class PreparedChunk:
    id: UUID
    index: int
    content_hash: bytes
    heading_path: tuple[str, ...]
    content: str
    token_count: int
    start_offset: int
    end_offset: int
    search_config: str
    simhash: int
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class PreparedDocument:
    normalized_text: str
    language: str
    token_count: int
    extracted_bytes: int
    chunks: tuple[PreparedChunk, ...]


def normalize_text(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    )
    normalized = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or (ord(character) >= 32 and ord(character) != 127)
    )
    normalized = "\n".join(
        _SPACE_PATTERN.sub(" ", line).rstrip() for line in normalized.split("\n")
    )
    return _BLANK_PATTERN.sub("\n\n", normalized).strip()


def tokens(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _TOKEN_PATTERN.finditer(value))


def detect_language(value: str) -> str:
    letters = [character for character in value if character.isalpha()]
    if not letters:
        return "und"
    ascii_letters = sum(character.isascii() for character in letters)
    return "en" if ascii_letters / len(letters) >= 0.85 else "und"


def simhash64(chunk_tokens: tuple[str, ...]) -> int:
    weights = [0] * 64
    for token in chunk_tokens:
        digest = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return result


def embed_text(value: str) -> tuple[float, ...]:
    """Return the deterministic local embedding used by indexing and search."""
    values: list[float] = []
    counter = 0
    while len(values) < EMBEDDING_DIMENSIONS:
        digest = hashlib.sha256(f"{counter}:{value}".encode()).digest()
        values.extend((byte - 127.5) / 127.5 for byte in digest)
        counter += 1
    vector = values[:EMBEDDING_DIMENSIONS]
    norm = math.sqrt(sum(component * component for component in vector))
    if not math.isfinite(norm) or norm == 0:
        raise IndexingError("EMBEDDING_INVALID")
    return tuple(component / norm for component in vector)


def version_key(
    *, source_id: UUID, content_hash: bytes, permissions_hash: bytes, profile_id: int
) -> bytes:
    value = b"\x00".join(
        (
            source_id.bytes,
            content_hash,
            permissions_hash,
            PARSER_VERSION.encode(),
            CHUNKER_VERSION.encode(),
            str(profile_id).encode(),
            EMBEDDING_MODEL.encode(),
        )
    )
    return hashlib.sha256(value).digest()


def document_version_id(source_id: UUID, key: bytes) -> UUID:
    return uuid5(source_id, key.hex())


def _paragraphs(value: str) -> list[tuple[tuple[str, ...], str, int, int]]:
    heading_path: list[str] = []
    result: list[tuple[tuple[str, ...], str, int, int]] = []
    position = 0
    for block in re.split(r"\n\s*\n", value):
        start = value.find(block, position)
        end = start + len(block)
        position = end
        heading = _HEADING_PATTERN.match(block)
        if heading:
            level = len(heading.group(1))
            heading_path = heading_path[: level - 1] + [heading.group(2)]
        result.append((tuple(heading_path), block, start, end))
    return result


def _split_large_block(
    heading: tuple[str, ...], block: str, start: int, maximum: int
) -> list[tuple[tuple[str, ...], str, int, int]]:
    block_tokens = list(_TOKEN_PATTERN.finditer(block))
    if len(block_tokens) <= maximum:
        return [(heading, block, start, start + len(block))]
    result = []
    for offset in range(0, len(block_tokens), maximum):
        group = block_tokens[offset : offset + maximum]
        local_start = group[0].start()
        local_end = group[-1].end()
        result.append(
            (
                heading,
                block[local_start:local_end],
                start + local_start,
                start + local_end,
            )
        )
    return result


def _chunk_blocks(
    value: str, target: int = 600, maximum: int = 800
) -> list[tuple[tuple[str, ...], str, int, int]]:
    blocks = [
        part
        for heading, block, start, _ in _paragraphs(value)
        for part in _split_large_block(heading, block, start, maximum)
    ]
    result: list[tuple[tuple[str, ...], str, int, int]] = []
    current: list[tuple[tuple[str, ...], str, int, int]] = []
    current_tokens = 0
    for block in blocks:
        count = len(tokens(block[1]))
        heading_changed = bool(current and current[-1][0] != block[0])
        if current and (
            current_tokens + count > maximum
            or (heading_changed and current_tokens >= target)
        ):
            result.append(_merge(current, value))
            current = []
            current_tokens = 0
        current.append(block)
        current_tokens += count
    if current:
        result.append(_merge(current, value))
    return result


def _merge(
    blocks: list[tuple[tuple[str, ...], str, int, int]], value: str
) -> tuple[tuple[str, ...], str, int, int]:
    start = blocks[0][2]
    end = blocks[-1][3]
    return blocks[0][0], value[start:end].strip(), start, end


class IndexingPipeline:
    def prepare(self, pending: PendingDocument) -> PreparedDocument:
        if not (
            pending.mime_type.startswith("text/")
            or pending.mime_type in _NORMALIZED_TEXT_MEDIA_TYPES
        ):
            raise IndexingError("UNSUPPORTED_MEDIA_TYPE")
        normalized = normalize_text(pending.content)
        if not normalized or not tokens(normalized):
            raise IndexingError("NO_INDEXABLE_TEXT")
        language = detect_language(normalized)
        search_config = "english" if language == "en" else "simple"
        unique_hashes: set[bytes] = set()
        scope_simhashes: dict[tuple[str, ...], list[int]] = {}
        chunks: list[PreparedChunk] = []
        for heading, content, start, end in _chunk_blocks(normalized):
            chunk_tokens = tokens(content)
            content_hash = hashlib.sha256(content.encode()).digest()
            fingerprint = simhash64(chunk_tokens)
            near_duplicate = any(
                (fingerprint ^ existing).bit_count() <= 3
                for existing in scope_simhashes.setdefault(heading, [])
            )
            if content_hash in unique_hashes or near_duplicate:
                continue
            index = len(chunks)
            unique_hashes.add(content_hash)
            scope_simhashes[heading].append(fingerprint)
            chunk_id = uuid5(
                pending.document_version_id,
                f"{CHUNKER_VERSION}:{index}:{content_hash.hex()}",
            )
            chunks.append(
                PreparedChunk(
                    id=chunk_id,
                    index=index,
                    content_hash=content_hash,
                    heading_path=heading,
                    content=content,
                    token_count=len(chunk_tokens),
                    start_offset=start,
                    end_offset=end,
                    search_config=search_config,
                    simhash=fingerprint,
                    embedding=embed_text(content),
                )
            )
            if len(chunks) > MAX_CHUNKS:
                raise IndexingError("CHUNK_LIMIT_EXCEEDED")
        if not chunks:
            raise IndexingError("NO_INDEXABLE_TEXT")
        return PreparedDocument(
            normalized_text=normalized,
            language=language,
            token_count=len(tokens(normalized)),
            extracted_bytes=len(normalized.encode()),
            chunks=tuple(chunks),
        )
