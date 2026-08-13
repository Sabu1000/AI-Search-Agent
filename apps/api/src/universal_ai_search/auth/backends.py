"""FastAPI authentication and workspace authorization adapters."""

from __future__ import annotations

from uuid import UUID

from fastapi import Request

from universal_ai_search.api.auth import (
    AuthenticationMode,
    Principal,
    WorkspaceContext,
)
from universal_ai_search.api.problems import ProblemError
from universal_ai_search.auth.service import AuthenticationService

ACCESS_COOKIE = "__Host-uas_access"
REFRESH_COOKIE = "__Host-uas_refresh"
CSRF_COOKIE = "__Host-uas_csrf"


class SessionAuthenticationBackend:
    def __init__(self, service: AuthenticationService) -> None:
        self._service = service

    async def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get("authorization")
        cookie_token = request.cookies.get(ACCESS_COOKIE)
        bearer_token: str | None = None
        if header:
            scheme, separator, value = header.partition(" ")
            if scheme.casefold() != "bearer" or not separator or not value:
                return None
            bearer_token = value
        if bearer_token and cookie_token:
            raise ProblemError(
                status=400,
                code="AMBIGUOUS_AUTHENTICATION",
                title="Authentication is ambiguous",
                detail="Use either browser cookies or a bearer token.",
            )
        token = bearer_token or cookie_token
        if token is None:
            return None
        authenticated = await self._service.authenticate_access(token)
        if authenticated is None:
            return None
        identity = authenticated.identity
        request.state.session_identity = identity
        return Principal(
            subject_id=identity.user_id,
            session_id=identity.session_id,
            mode=(
                AuthenticationMode.BEARER
                if bearer_token
                else AuthenticationMode.BROWSER
            ),
            authorization_version=identity.authorization_version,
            access_expires_at=authenticated.access_expires_at,
        )


class DatabaseWorkspaceAuthorizationBackend:
    def __init__(self, service: AuthenticationService) -> None:
        self._service = service

    async def authorize(
        self, *, principal: Principal, workspace_id: UUID
    ) -> WorkspaceContext | None:
        membership = await self._service.store.membership(
            user_id=principal.subject_id, workspace_id=workspace_id
        )
        if membership is None:
            return None
        return WorkspaceContext(
            workspace_id=membership.workspace_id,
            user_id=principal.subject_id,
            role=membership.role,
            authorization_version=membership.authorization_version,
        )
