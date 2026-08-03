from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from universal_ai_search import __version__
from universal_ai_search.config import Settings, get_settings
from universal_ai_search.platform.readiness import (
    ReadinessResponse,
    check_readiness,
)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"service": "api", "status": "ok", "version": __version__}


@router.get("/ready", response_model=ReadinessResponse)
async def readiness(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadinessResponse | JSONResponse:
    result = await check_readiness(settings)
    if result.status == "degraded":
        return JSONResponse(status_code=503, content=result.model_dump())
    return result
