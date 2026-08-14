"""Versioned public router.

Feature routers are included here only after their services and acceptance
paths exist.
"""

from fastapi import APIRouter

from universal_ai_search.api.routes.auth import router as auth_router
from universal_ai_search.api.routes.connections import router as connections_router
from universal_ai_search.api.routes.search import router as search_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
router.include_router(connections_router)
router.include_router(search_router)
