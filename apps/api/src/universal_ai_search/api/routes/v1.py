"""Versioned public router.

Feature routers are included here only after their services and acceptance
paths exist. B2 intentionally exposes no placeholder product endpoints.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/v1")
