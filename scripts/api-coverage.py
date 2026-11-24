#!/usr/bin/env python3
"""Report wired OpenAPI operations against the versioned API catalog.

Run with the backend environment. A wired endpoint may be feature-gated or
support a narrower initial subset; this is not end-to-end acceptance.
"""

from __future__ import annotations

import re
from pathlib import Path

from universal_ai_search.api.app import create_app

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "universal_ai_search_documentation" / "05_API_SPECIFICATION.md"
ROW = re.compile(
    r"^\| `(GET|POST|PUT|PATCH|DELETE)` \| `(/v1/[^\x60]+)` \|", re.MULTILINE
)


def main() -> None:
    expected = sorted(set(ROW.findall(CATALOG.read_text(encoding="utf-8"))))
    schema = create_app().openapi()
    paths = schema["paths"]
    wired = [
        (method, path)
        for method, path in expected
        if method.lower() in paths.get(path, {})
    ]
    unwired = sorted(set(expected) - set(wired))
    if not expected:
        raise RuntimeError("No API operations found in versioned catalog")
    print(f"{len(wired)}/{len(expected)} wired ({len(wired) / len(expected):.0%})")
    print(
        f"{len(unwired)}/{len(expected)} unwired ({len(unwired) / len(expected):.0%})"
    )
    for method, path in unwired:
        print(f"  {method} {path}")


if __name__ == "__main__":
    main()
