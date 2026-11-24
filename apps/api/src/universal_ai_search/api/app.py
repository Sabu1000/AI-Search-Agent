from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from universal_ai_search import __version__
from universal_ai_search.api.context import RequestContextMiddleware
from universal_ai_search.api.pagination import CursorSigner
from universal_ai_search.api.problems import install_problem_handlers
from universal_ai_search.api.routes.health import router as health_router
from universal_ai_search.api.routes.v1 import router as v1_router
from universal_ai_search.auth.backends import (
    DatabaseWorkspaceAuthorizationBackend,
    SessionAuthenticationBackend,
)
from universal_ai_search.auth.email import SMTPVerificationSender
from universal_ai_search.auth.reauth import ReauthenticationService, RedisReauthLimiter
from universal_ai_search.auth.reauth_store import ReauthenticationStore
from universal_ai_search.auth.recovery import PasswordRecoveryService
from universal_ai_search.auth.recovery_email import (
    BoundedRecoveryMailQueue,
    SMTPRecoveryMailer,
)
from universal_ai_search.auth.recovery_limiter import (
    RedisRecoveryRequestLimiter,
    RedisScriptClient,
)
from universal_ai_search.auth.recovery_store import SQLAlchemyRecoveryStore
from universal_ai_search.auth.security import AccessTokenCodec, PasswordSecurity
from universal_ai_search.auth.service import AuthenticationService
from universal_ai_search.auth.store import SQLAlchemyAuthStore
from universal_ai_search.config import Settings, get_settings
from universal_ai_search.connections.crypto import LocalEnvelopeEncryption
from universal_ai_search.connections.github import HttpGitHubAppGateway
from universal_ai_search.connections.github_event_store import (
    SQLAlchemyGitHubWebhookService,
)
from universal_ai_search.connections.github_service import GitHubConnectionService
from universal_ai_search.connections.github_store import SQLAlchemyGitHubConnectionStore
from universal_ai_search.connections.google import HttpGoogleGateway
from universal_ai_search.connections.mutations import SQLAlchemyConnectionMutationStore
from universal_ai_search.connections.query import SQLAlchemyConnectionQueryReader
from universal_ai_search.connections.service import GoogleConnectionService
from universal_ai_search.connections.store import SQLAlchemyGoogleConnectionStore
from universal_ai_search.conversations.deletion import ConversationDeletionStore
from universal_ai_search.conversations.repository import ConversationRepository
from universal_ai_search.devices.heartbeat import DeviceHeartbeatService
from universal_ai_search.devices.heartbeat_store import SQLAlchemyDeviceHeartbeatStore
from universal_ai_search.devices.manifest_store import SQLAlchemyDeviceManifestStore
from universal_ai_search.devices.manifests import DeviceManifestService
from universal_ai_search.devices.query import SQLAlchemyDeviceQueryReader
from universal_ai_search.devices.registration import DeviceRegistrationService
from universal_ai_search.devices.registration_store import (
    SQLAlchemyDeviceRegistrationStore,
)
from universal_ai_search.search.history import SearchHistoryStore
from universal_ai_search.search.repository import SearchRepository
from universal_ai_search.search.service import SearchService
from universal_ai_search.search.source_detail import SourceDetailRepository


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    recovery_mail = getattr(app.state, "recovery_mail", None)
    if isinstance(recovery_mail, BoundedRecoveryMailQueue):
        await recovery_mail.drain()
    recovery_redis = getattr(app.state, "recovery_redis", None)
    if isinstance(recovery_redis, Redis):
        await recovery_redis.aclose()
    engine = getattr(app.state, "database_engine", None)
    if isinstance(engine, AsyncEngine):
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    app = FastAPI(
        title="Universal AI Search API",
        summary="Read-only, cited search across user-approved sources.",
        version=__version__,
        lifespan=_lifespan,
    )
    app.openapi_version = "3.1.0"
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
    redis = Redis.from_url(runtime_settings.redis_url, socket_timeout=2)
    app.state.recovery_redis = redis
    app.state.reauth_service = ReauthenticationService(
        store=ReauthenticationStore(engine),
        security=service.password_security,
        limiter=RedisReauthLimiter(cast(RedisScriptClient, redis)),
        hash_key=runtime_settings.auth_hash_key.get_secret_value().encode(),
    )
    app.state.password_recovery_service = None
    if runtime_settings.password_recovery_enabled:
        mail_queue = BoundedRecoveryMailQueue(
            SMTPRecoveryMailer(
                host=runtime_settings.smtp_host,
                port=runtime_settings.smtp_port,
                sender=runtime_settings.email_from,
                require_starttls=runtime_settings.environment not in {"local", "test"},
            )
        )
        app.state.recovery_mail = mail_queue
        app.state.password_recovery_service = PasswordRecoveryService(
            store=SQLAlchemyRecoveryStore(engine),
            security=PasswordSecurity(),
            limiter=RedisRecoveryRequestLimiter(cast(RedisScriptClient, redis)),
            mail=mail_queue,
            token_hash_key=runtime_settings.auth_hash_key.get_secret_value().encode(),
            web_origin=runtime_settings.web_origin,
        )
    app.state.database_engine = engine
    app.state.authentication_service = service
    app.state.authentication_backend = SessionAuthenticationBackend(service)
    app.state.workspace_authorization_backend = DatabaseWorkspaceAuthorizationBackend(
        service
    )
    app.state.search_service = SearchService(SearchRepository(engine))
    app.state.source_detail_repository = SourceDetailRepository(engine)
    app.state.source_cursor_signer = CursorSigner(
        runtime_settings.cursor_signing_key.get_secret_value().encode()
    )
    app.state.search_history_store = SearchHistoryStore(
        engine,
        idempotency_secret=runtime_settings.idempotency_hash_key.get_secret_value().encode(),
        audit_secret=runtime_settings.auth_hash_key.get_secret_value().encode(),
    )
    app.state.device_query_reader = SQLAlchemyDeviceQueryReader(engine)
    app.state.device_cursor_signer = CursorSigner(
        runtime_settings.cursor_signing_key.get_secret_value().encode()
    )
    app.state.device_registration_service = None
    app.state.device_heartbeat_service = None
    app.state.device_manifest_service = None
    if runtime_settings.desktop_registration_enabled:
        registration_key = runtime_settings.auth_hash_key.get_secret_value().encode()
        app.state.device_registration_service = DeviceRegistrationService(
            SQLAlchemyDeviceRegistrationStore(engine, audit_key=registration_key),
            hash_key=registration_key,
        )
        app.state.device_heartbeat_service = DeviceHeartbeatService(
            SQLAlchemyDeviceHeartbeatStore(engine), hash_key=registration_key
        )
        app.state.device_manifest_service = DeviceManifestService(
            SQLAlchemyDeviceManifestStore(engine), hash_key=registration_key
        )
    app.state.connection_query_reader = SQLAlchemyConnectionQueryReader(engine)
    app.state.connection_mutation_store = SQLAlchemyConnectionMutationStore(
        engine,
        idempotency_secret=runtime_settings.idempotency_hash_key.get_secret_value().encode(),
        audit_secret=runtime_settings.auth_hash_key.get_secret_value().encode(),
        google_sync_enabled=runtime_settings.google_oauth_enabled,
        github_sync_enabled=runtime_settings.github_app_enabled,
    )
    app.state.connection_cursor_signer = CursorSigner(
        runtime_settings.cursor_signing_key.get_secret_value().encode()
    )
    app.state.conversation_repository = ConversationRepository(engine)
    app.state.conversation_deletion_store = ConversationDeletionStore(
        engine,
        idempotency_secret=(
            runtime_settings.idempotency_hash_key.get_secret_value().encode()
        ),
        audit_secret=runtime_settings.auth_hash_key.get_secret_value().encode(),
    )
    app.state.conversation_cursor_signer = CursorSigner(
        runtime_settings.cursor_signing_key.get_secret_value().encode()
    )
    app.state.conversation_idempotency_key = (
        runtime_settings.idempotency_hash_key.get_secret_value().encode()
    )
    app.state.google_connection_service = GoogleConnectionService(
        store=SQLAlchemyGoogleConnectionStore(engine),
        gateway=HttpGoogleGateway(
            client_id=runtime_settings.google_client_id,
            client_secret=runtime_settings.google_client_secret.get_secret_value(),
            redirect_uri=runtime_settings.google_redirect_uri,
        ),
        encryption=LocalEnvelopeEncryption(
            runtime_settings.provider_encryption_key.get_secret_value().encode()
        ),
        hash_key=runtime_settings.oauth_hash_key.get_secret_value().encode(),
        enabled=runtime_settings.google_oauth_enabled,
    )
    app.state.github_connection_service = None
    app.state.github_webhook_service = None
    if runtime_settings.github_app_enabled:
        webhook_secret = (
            runtime_settings.github_webhook_secret.get_secret_value().encode()
        )
        if webhook_secret:
            if len(webhook_secret) < 32:
                raise ValueError("GitHub webhook secret must be at least 32 bytes")
            app.state.github_webhook_secret = webhook_secret
            app.state.github_webhook_service = SQLAlchemyGitHubWebhookService(engine)
    if runtime_settings.github_app_enabled:
        app.state.github_connection_service = GitHubConnectionService(
            store=SQLAlchemyGitHubConnectionStore(
                engine,
                audit_key=runtime_settings.auth_hash_key.get_secret_value().encode(),
            ),
            gateway=HttpGitHubAppGateway(
                app_id=runtime_settings.github_app_id,
                private_key_pem=(
                    runtime_settings.github_private_key_pem.get_secret_value().encode()
                ),
            ),
            encryption=LocalEnvelopeEncryption(
                runtime_settings.provider_encryption_key.get_secret_value().encode()
            ),
            hash_key=runtime_settings.oauth_hash_key.get_secret_value().encode(),
            app_slug=runtime_settings.github_app_slug,
            client_id=runtime_settings.github_client_id,
            client_secret=runtime_settings.github_client_secret.get_secret_value(),
            redirect_uri=runtime_settings.github_redirect_uri,
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[runtime_settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        expose_headers=["ETag"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Confirm-Action",
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
