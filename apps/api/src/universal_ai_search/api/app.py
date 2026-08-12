from fastapi import FastAPI

from universal_ai_search import __version__
from universal_ai_search.api.auth import (
    RejectingAuthenticationBackend,
    RejectingWorkspaceAuthorizationBackend,
)
from universal_ai_search.api.context import RequestContextMiddleware
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.health import router as health_router
from universal_ai_search.api.routes.v1 import router as v1_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Universal AI Search API",
        summary="Read-only, cited search across user-approved sources.",
        version=__version__,
        openapi_version="3.1.0",
    )
    app.state.authentication_backend = RejectingAuthenticationBackend()
    app.state.workspace_authorization_backend = RejectingWorkspaceAuthorizationBackend()
    app.add_middleware(RequestContextMiddleware)
    install_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(v1_router)
    return app


app = create_app()
