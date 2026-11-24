"""Connect separately verified and version-pinned uploads to real indexing."""

from pathlib import Path

from alembic import op

revision = "0023_device_upload_index"
down_revision = "0022_device_upload_completion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0023_device_upload_index.sql"
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        sql.read_text(encoding="utf-8")
    )


def downgrade() -> None:
    # Do not strand staged local sources under a worker without the provenance
    # fence. Clean up retained local derivatives explicitly before downgrading.
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM app.sources
                       WHERE provider='local_files' AND metadata ? 'local_upload_id')
            THEN RAISE EXCEPTION 'staged source cleanup required before downgrade';
            END IF;
        END $$"""
    )
    for function in (
        "read_device_upload_index_operation(UUID,UUID,UUID,UUID,BIGINT,BIGINT)",
        "verify_staged_source_index_scope(UUID,UUID,BOOLEAN)",
        "finish_verified_device_upload(UUID,UUID,TEXT,INTEGER,UUID,TEXT,BOOLEAN)",
        "load_verified_device_upload(UUID,UUID,TEXT,INTEGER,BOOLEAN)",
        "claim_verified_device_upload(TEXT)",
    ):
        op.execute(f"DROP FUNCTION app.{function}")
    op.execute("DROP INDEX app.ix_device_upload_index_ready")
    op.execute("DROP INDEX app.uq_device_upload_index_job")
    op.execute(
        "ALTER TABLE app.device_upload_changes "
        "DROP CONSTRAINT ck_device_upload_index_job"
    )
    op.execute(
        "ALTER TABLE app.device_upload_changes "
        "DROP CONSTRAINT ck_device_upload_index_state"
    )
    op.execute(
        """ALTER TABLE app.device_upload_changes DROP COLUMN index_status,
               DROP COLUMN index_attempt,DROP COLUMN index_lease_owner,
               DROP COLUMN index_lease_until,DROP COLUMN index_available_at,
               DROP COLUMN index_started_at,DROP COLUMN index_finished_at,
               DROP COLUMN index_error_code,DROP COLUMN index_job_id"""
    )
