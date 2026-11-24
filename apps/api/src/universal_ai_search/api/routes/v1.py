"""Versioned public router.

Feature routers are included here only after their services and acceptance
paths exist.
"""

from fastapi import APIRouter

from universal_ai_search.api.routes.auth import router as auth_router
from universal_ai_search.api.routes.connection_disconnect import (
    router as connection_disconnect_router,
)
from universal_ai_search.api.routes.connection_mutations import (
    router as connection_mutation_router,
)
from universal_ai_search.api.routes.connection_queries import (
    router as connection_query_router,
)
from universal_ai_search.api.routes.connections import router as connections_router
from universal_ai_search.api.routes.conversation_deletion import (
    operations_router as conversation_operations_router,
)
from universal_ai_search.api.routes.conversation_deletion import (
    router as conversation_delete_router,
)
from universal_ai_search.api.routes.conversation_stream import (
    router as conversation_stream_router,
)
from universal_ai_search.api.routes.conversations import router as conversation_router
from universal_ai_search.api.routes.device_heartbeat import router as heartbeat_router
from universal_ai_search.api.routes.device_manifests import (
    router as device_manifest_router,
)
from universal_ai_search.api.routes.device_queries import router as device_query_router
from universal_ai_search.api.routes.device_registration import router as device_router
from universal_ai_search.api.routes.device_revocation import (
    router as device_revocation_router,
)
from universal_ai_search.api.routes.device_uploads import router as device_upload_router
from universal_ai_search.api.routes.github_connections import router as github_router
from universal_ai_search.api.routes.github_webhooks import (
    router as github_webhook_router,
)
from universal_ai_search.api.routes.job_operations import router as job_operation_router
from universal_ai_search.api.routes.local_account_deletion import (
    router as local_account_delete_router,
)
from universal_ai_search.api.routes.local_account_export import (
    router as local_account_export_router,
)
from universal_ai_search.api.routes.password_recovery import (
    router as password_recovery_router,
)
from universal_ai_search.api.routes.reauthentication import router as reauth_router
from universal_ai_search.api.routes.search import router as search_router
from universal_ai_search.api.routes.search_history import (
    router as search_history_router,
)
from universal_ai_search.api.routes.source_deletion import (
    router as source_deletion_router,
)
from universal_ai_search.api.routes.source_detail import router as source_detail_router
from universal_ai_search.api.routes.source_reindex import (
    router as source_reindex_router,
)
from universal_ai_search.api.routes.verification_resend import (
    router as verification_resend_router,
)
from universal_ai_search.api.routes.workspace_usage import router as usage_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
router.include_router(password_recovery_router)
router.include_router(connections_router)
router.include_router(github_router)
router.include_router(github_webhook_router)
router.include_router(connection_query_router)
router.include_router(conversation_router)
router.include_router(conversation_delete_router)
router.include_router(conversation_stream_router)
router.include_router(conversation_operations_router)
router.include_router(search_router)
router.include_router(device_router)
router.include_router(heartbeat_router)
router.include_router(reauth_router)
router.include_router(usage_router)
router.include_router(job_operation_router)
router.include_router(device_query_router)
router.include_router(search_history_router)
router.include_router(source_detail_router)
router.include_router(device_manifest_router)
router.include_router(device_upload_router)
router.include_router(connection_mutation_router)
router.include_router(device_revocation_router)
router.include_router(verification_resend_router)
router.include_router(connection_disconnect_router)
router.include_router(local_account_delete_router)
router.include_router(source_reindex_router)
router.include_router(source_deletion_router)
router.include_router(local_account_export_router)
