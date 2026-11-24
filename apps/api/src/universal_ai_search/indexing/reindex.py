"""Fresh authorization fence for explicitly requested local index rebuilds."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from universal_ai_search.search.repository import _ELIGIBLE

from .pipeline import IndexingError

if TYPE_CHECKING:
    import psycopg

    from .repository import ClaimedJob


def require_current_reindex(
    connection: psycopg.Connection[dict[str, object]],
    claim: ClaimedJob,
    *,
    promoting: bool = False,
) -> None:
    """Ordinary provider-index jobs do not have manual-reindex lineage."""
    lease = connection.execute(
        """SELECT payload ->> 'source_reindex_actor' AS actor
           FROM app.jobs
           WHERE id = %s AND workspace_id = %s AND status = 'leased'
             AND lease_owner = %s AND attempt_count = %s
             AND lease_expires_at > clock_timestamp()""",
        (claim.job_id, claim.workspace_id, claim.worker_id, claim.attempt_number),
    ).fetchone()
    if lease is None or lease["actor"] is None:
        return
    actor_id = UUID(str(lease["actor"]))
    connection.execute("SELECT set_config('app.user_id', %s, true)", (str(actor_id),))
    # Lock stable authorization rows only at publication. NOWAIT prevents a
    # conflicting provider update from becoming a lock inversion with indexing.
    workspace_lock = " FOR NO KEY UPDATE OF workspace NOWAIT" if promoting else ""
    member_lock = " FOR SHARE OF actor, member, connection NOWAIT" if promoting else ""
    lock_values = {
        "workspace_id": claim.workspace_id,
        "actor_id": actor_id,
        "job_id": claim.job_id,
    }
    if promoting:
        connection.execute(
            "SELECT id FROM app.workspaces AS workspace WHERE id = %(workspace_id)s"
            + workspace_lock,
            lock_values,
        )
    eligible = _ELIGIBLE.replace(":workspace_id", "%(workspace_id)s")
    row = connection.execute(
        f"""SELECT true
        FROM app.jobs AS job
        JOIN app.sources AS source ON source.workspace_id = job.workspace_id
         AND source.id = job.source_id
        JOIN app.document_versions AS version
          ON version.workspace_id = source.workspace_id
         AND version.source_id = source.id
         AND version.id::text = job.payload ->> 'expected_current_document_version_id'
        JOIN app.connections AS connection
          ON connection.workspace_id = source.workspace_id
         AND connection.id = source.connection_id
        JOIN app.workspaces AS workspace ON workspace.id = source.workspace_id
        JOIN app.workspace_members AS member ON member.workspace_id = workspace.id
         AND member.user_id = connection.owner_user_id
        JOIN app.users AS actor ON actor.id = member.user_id
        JOIN app.embedding_profiles AS profile
         ON profile.id = (job.payload ->> 'embedding_profile_id')::integer
        WHERE job.id = %(job_id)s AND actor.id = %(actor_id)s
          AND {eligible}
          AND source.content_hash = version.content_hash
          AND version.object_storage_key IS NULL
          AND job.created_at > clock_timestamp() - INTERVAL '24 hours'
          AND workspace.status = 'active'
          AND workspace.authorization_version::text
              = job.payload ->> 'workspace_authorization_version'
          AND actor.status = 'active' AND actor.email_verified_at IS NOT NULL
          AND actor.lock_version::text = job.payload ->> 'user_authorization_version'
          AND member.status = 'active' AND profile.status = 'active'
        """
        + member_lock,
        lock_values,
    ).fetchone()
    if row is None:
        raise IndexingError("AUTHORIZATION_CHANGED")
