import argparse
import json
import socket
import time
from collections.abc import Sequence

from universal_ai_search import __version__
from universal_ai_search.config import get_settings
from universal_ai_search.indexing.pipeline import IndexingPipeline
from universal_ai_search.indexing.repository import IndexRepository
from universal_ai_search.indexing.runtime import IndexingRuntime


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
    runtime = IndexingRuntime(
        IndexRepository(settings.database_url), IndexingPipeline()
    )
    worker_id = f"{socket.gethostname()}:{__version__}"
    if arguments.once:
        runtime.run_once(worker_id)
        return 0
    while True:
        consumed = runtime.run_once(worker_id)
        if not consumed:
            time.sleep(arguments.poll_interval)


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover - exercised by the console entry point.
    run()
