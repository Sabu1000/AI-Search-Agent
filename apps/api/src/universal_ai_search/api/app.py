from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from universal_ai_search import __version__
from universal_ai_search.api.context import RequestContextMiddleware
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.health import router as health_router
from universal_ai_search.api.routes.v1 import router as v1_router
from universal_ai_search.auth.backends import (
    DatabaseWorkspaceAuthorizationBackend,
    SessionAuthenticationBackend,
)
from universal_ai_search.auth.email import SMTPVerificationSender
from universal_ai_search.auth.security import AccessTokenCodec, PasswordSecurity
from universal_ai_search.auth.service import AuthenticationService
from universal_ai_search.auth.store import SQLAlchemyAuthStore
from universal_ai_search.config import Settings, get_settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    engine = getattr(app.state, "database_engine", None)
    if isinstance(engine, AsyncEngine):
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    app = FastAPI(
        title="Universal AI Search API",
        summary="Read-only, cited search across user-approved sources.",
        version=__version__,
        openapi_version="3.1.0",
        lifespan=_lifespan,
    )
    engine = create_async_engine(
        runtime_settings.database_url,
        pool_pre_ping=True,
        connect_args={"server_settings": {"role": runtime_settings.database_role}},
    )
    store = SQLAlchemyAuthStore(engine)
    service = AuthenticationService(
        store=store,
        password_security=PasswordSecurity(),
        token_codec=AccessTokenCodec(
            runtime_settings.auth_signing_key.get_secret_value().encode()
        ),
        token_hash_key=runtime_settings.auth_hash_key.get_secret_value().encode(),
        verification_sender=SMTPVerificationSender(
            host=runtime_settings.smtp_host,
            port=runtime_settings.smtp_port,
            sender=runtime_settings.email_from,
        ),
        web_origin=runtime_settings.web_origin,
    )
    app.state.database_engine = engine
    app.state.authentication_service = service
    app.state.authentication_backend = SessionAuthenticationBackend(service)
    app.state.workspace_authorization_backend = DatabaseWorkspaceAuthorizationBackend(
        service
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[runtime_settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-CSRF-Token",
            "X-Request-ID",
            "X-Workspace-ID",
        ],
    )
    app.add_middleware(RequestContextMiddleware)
    install_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(v1_router)
    return app


app = create_app()
