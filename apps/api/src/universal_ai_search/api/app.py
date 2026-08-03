from fastapi import FastAPI

from universal_ai_search import __version__
from universal_ai_search.api.routes.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Universal AI Search API",
        summary="Read-only, cited search across user-approved sources.",
        version=__version__,
    )
    app.include_router(health_router)
    return app


app = create_app()
