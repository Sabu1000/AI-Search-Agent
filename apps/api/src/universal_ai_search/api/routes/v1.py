"""Versioned public router.

Feature routers are included here only after their services and acceptance
paths exist. B2 intentionally exposes no placeholder product endpoints.
"""

from fastapi import APIRouter

from universal_ai_search.api.routes.auth import router as auth_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
