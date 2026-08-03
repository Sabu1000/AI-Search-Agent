import argparse
import json
from collections.abc import Sequence

from universal_ai_search import __version__
from universal_ai_search.config import get_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Universal AI Search worker")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit without consuming jobs",
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
    raise SystemExit("Job consumption is introduced with the indexing pipeline.")


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover - exercised by the console entry point.
    run()
