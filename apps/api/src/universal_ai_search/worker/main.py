import argparse
import json
import socket
import time
from collections.abc import Sequence

from universal_ai_search import __version__
from universal_ai_search.config import get_settings
from universal_ai_search.connections.crypto import LocalEnvelopeEncryption
from universal_ai_search.connections.drive import HttpDriveClient
from universal_ai_search.connections.gmail import HttpGmailClient
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime
from universal_ai_search.sync.drive_repository import DriveSyncRepository
from universal_ai_search.sync.drive_runtime import DriveSyncRuntime
from universal_ai_search.sync.repository import GoogleSyncRepository
from universal_ai_search.sync.runtime import GmailSyncRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Universal AI Search worker")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit without consuming jobs",
    )
    parser.add_argument(
        "--once", action="store_true", help="consume at most one job and exit"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="seconds to wait when the durable queue is empty",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    if arguments.check:
        print(
            json.dumps(
                {
                    "environment": settings.environment,
                    "service": "worker",
                    "status": "ok",
                    "version": __version__,
                },
                sort_keys=True,
            )
        )
        return 0
    if arguments.poll_interval <= 0:
        raise SystemExit("--poll-interval must be greater than zero")
    index_repository = IndexRepository(settings.database_url)
    runtime = IndexingRuntime(index_repository, IndexingPipeline())
    sync_runtime = GmailSyncRuntime(
        repository=GoogleSyncRepository(settings.database_url),
        index_repository=index_repository,
        client=HttpGmailClient(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value(),
        ),
        encryption=LocalEnvelopeEncryption(
            settings.provider_encryption_key.get_secret_value().encode()
        ),
        enabled=settings.google_oauth_enabled,
    )
    drive_sync_runtime = DriveSyncRuntime(
        repository=DriveSyncRepository(settings.database_url),
        index_repository=index_repository,
        client=HttpDriveClient(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value(),
        ),
        encryption=LocalEnvelopeEncryption(
            settings.provider_encryption_key.get_secret_value().encode()
        ),
        enabled=settings.google_oauth_enabled,
    )
    worker_id = f"{socket.gethostname()}:{__version__}"
    if arguments.once:
        if not sync_runtime.run_once(worker_id) and not drive_sync_runtime.run_once(
            worker_id
        ):
            runtime.run_once(worker_id)
        return 0
    while True:
        synced = sync_runtime.run_once(worker_id)
        drive_synced = drive_sync_runtime.run_once(worker_id)
        indexed = runtime.run_once(worker_id)
        consumed = synced or drive_synced or indexed
        if not consumed:
            time.sleep(arguments.poll_interval)


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover - exercised by the console entry point.
    run()
