//! Per-selection SQLite baseline. Planning never mutates it; a downstream
//! acknowledgement is required before committing the new baseline.

use std::collections::{BTreeMap, HashSet};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, TransactionBehavior};

use crate::{
    permitted_path_component, ManifestRecord, SelectedRoot, Snapshot, MAX_ENTRIES, MAX_FILE_BYTES,
    MAX_PATH_BYTES,
};

const SCHEMA_VERSION: i64 = 1;
const MAX_DATABASE_BYTES: i64 = 64 * 1024 * 1024;
type Digest = [u8; 32];

const SCHEMA: &str = "
    CREATE TABLE baseline (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        store_id BLOB NOT NULL CHECK (length(store_id) = 32),
        root_scope BLOB NOT NULL CHECK (length(root_scope) = 32),
        revision INTEGER NOT NULL CHECK (revision >= 0)
    ) STRICT;
    CREATE TABLE manifest_files (
        external_id BLOB PRIMARY KEY CHECK (length(external_id) = 32),
        relative_path TEXT NOT NULL UNIQUE
            CHECK (length(CAST(relative_path AS BLOB)) BETWEEN 1 AND 1024),
        path_sha256 BLOB NOT NULL CHECK (length(path_sha256) = 32),
        size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 100000000),
        modified_before_epoch INTEGER NOT NULL CHECK (modified_before_epoch IN (0, 1)),
        modified_secs INTEGER NOT NULL CHECK (modified_secs >= 0),
        modified_nanos INTEGER NOT NULL CHECK (modified_nanos BETWEEN 0 AND 999999999),
        content_sha256 BLOB NOT NULL CHECK (length(content_sha256) = 32),
        mime_type TEXT NOT NULL CHECK (mime_type IN ('text/plain', 'text/markdown', 'text/csv'))
    ) WITHOUT ROWID, STRICT;
";

/// Never carries a filename, DB path, or underlying SQLite message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManifestError {
    UnsupportedPlatform,
    IncompleteSnapshot,
    InvalidSnapshot,
    RootMismatch,
    IncompatibleDatabase,
    InvalidBaseline,
    StalePlan,
    StorageUnavailable,
}

impl From<rusqlite::Error> for ManifestError {
    fn from(_: rusqlite::Error) -> Self {
        Self::StorageUnavailable
    }
}

/// Local-only delta entries. No absolute paths or file contents are stored.
#[derive(Clone, PartialEq, Eq)]
pub enum ManifestChange {
    Added(ManifestRecord),
    Modified(ManifestRecord),
    Renamed {
        previous_relative_path: String,
        record: ManifestRecord,
        content_changed: bool,
    },
    Removed {
        external_id: Digest,
        relative_path: String,
    },
}

/// Opaque plan bound to one database incarnation and the revision it read.
pub struct ManifestPlan {
    binding: Binding,
    records: Vec<ManifestRecord>,
    changes: Vec<ManifestChange>,
}

impl ManifestPlan {
    /// Changes have deterministic order by external ID.
    pub fn changes(&self) -> &[ManifestChange] {
        &self.changes
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct Binding {
    store_id: Digest,
    root_scope: Digest,
    revision: i64,
}

pub struct ManifestStore {
    database: Connection,
    root_scope: Digest,
}

impl ManifestStore {
    /// Take ownership of a dedicated SQLite connection to this root's private
    /// app-data file. The caller must create and protect that file through the
    /// platform's app-data controls; never use a file selected for scanning.
    /// The scanner itself remains read-only with respect to selected roots.
    pub fn from_connection(
        database: Connection,
        root: &SelectedRoot,
    ) -> Result<Self, ManifestError> {
        Self::for_scope(database, root.scope()?)
    }

    fn for_scope(mut database: Connection, root_scope: Digest) -> Result<Self, ManifestError> {
        database.busy_timeout(Duration::from_secs(2))?;
        database.execute_batch(
            "PRAGMA trusted_schema=OFF;
             PRAGMA journal_mode=DELETE;
             PRAGMA secure_delete=ON;
             PRAGMA temp_store=MEMORY;",
        )?;
        let page_size: i64 = database.pragma_query_value(None, "page_size", |row| row.get(0))?;
        if !(512..=65_536).contains(&page_size) {
            return Err(ManifestError::IncompatibleDatabase);
        }
        let max_pages = MAX_DATABASE_BYTES / page_size;
        let effective_limit: i64 =
            database
                .pragma_update_and_check(None, "max_page_count", max_pages, |row| row.get(0))?;
        if !(1..=max_pages).contains(&effective_limit) {
            return Err(ManifestError::IncompatibleDatabase);
        }

        let transaction = database.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let version: i64 =
            transaction.pragma_query_value(None, "user_version", |row| row.get(0))?;
        match version {
            0 => {
                let objects: i64 = transaction.query_row(
                    "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'",
                    [],
                    |row| row.get(0),
                )?;
                if objects != 0 {
                    return Err(ManifestError::IncompatibleDatabase);
                }
                transaction.execute_batch(SCHEMA)?;
                transaction.execute(
                    "INSERT INTO baseline VALUES (1, randomblob(32), ?1, 0)",
                    params![root_scope],
                )?;
                transaction.pragma_update(None, "user_version", SCHEMA_VERSION)?;
            }
            SCHEMA_VERSION => {}
            _ => return Err(ManifestError::IncompatibleDatabase),
        }
        if binding(&transaction)?.root_scope != root_scope {
            return Err(ManifestError::RootMismatch);
        }
        transaction.commit()?;
        Ok(Self {
            database,
            root_scope,
        })
    }

    /// Plan only from a complete, authoritative scan for this same root.
    /// No additions, changes, or removals from a partial scan can advance state.
    pub fn plan(&mut self, snapshot: Snapshot) -> Result<ManifestPlan, ManifestError> {
        if !snapshot.authoritative {
            return Err(ManifestError::IncompleteSnapshot);
        }
        if snapshot.root_scope != self.root_scope {
            return Err(ManifestError::RootMismatch);
        }
        validate_records(&snapshot.records, ManifestError::InvalidSnapshot)?;
        let transaction = self.database.transaction()?;
        let binding = binding(&transaction)?;
        if binding.root_scope != self.root_scope {
            return Err(ManifestError::RootMismatch);
        }
        let baseline = read_records(&transaction)?;
        transaction.commit()?;
        let changes = diff(&baseline, &snapshot.records);
        Ok(ManifestPlan {
            binding,
            records: snapshot.records,
            changes,
        })
    }

    /// Commit only after all changes in the plan were accepted downstream.
    /// A stale plan or failed SQLite transaction leaves the baseline intact.
    pub fn acknowledge(&mut self, plan: ManifestPlan) -> Result<(), ManifestError> {
        let transaction = self
            .database
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        if plan.binding.root_scope != self.root_scope || binding(&transaction)? != plan.binding {
            return Err(ManifestError::StalePlan);
        }
        if !plan.changes.is_empty() {
            // Replacing a bounded snapshot keeps arbitrary path swaps atomic
            // without temporary path markers or unique-constraint collisions.
            transaction.execute("DELETE FROM manifest_files", [])?;
            {
                let mut insert = transaction.prepare(
                    "INSERT INTO manifest_files (
                         external_id, relative_path, path_sha256, size_bytes,
                         modified_before_epoch, modified_secs, modified_nanos,
                         content_sha256, mime_type
                     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                )?;
                for record in plan.records {
                    let (before, seconds, nanos) = encode_timestamp(record.modified_at)
                        .ok_or(ManifestError::InvalidSnapshot)?;
                    insert.execute(params![
                        record.external_id,
                        record.relative_path,
                        record.absolute_path_sha256,
                        i64::try_from(record.size_bytes)
                            .map_err(|_| ManifestError::InvalidSnapshot)?,
                        before,
                        seconds,
                        nanos,
                        record.content_sha256,
                        record.mime_type
                    ])?;
                }
            }
            let next = plan
                .binding
                .revision
                .checked_add(1)
                .ok_or(ManifestError::InvalidBaseline)?;
            transaction.execute(
                "UPDATE baseline SET revision = ?1 WHERE singleton = 1",
                [next],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }
}

fn binding(database: &Connection) -> Result<Binding, ManifestError> {
    let binding = database.query_row(
        "SELECT store_id, root_scope, revision FROM baseline WHERE singleton = 1",
        [],
        |row| {
            Ok(Binding {
                store_id: row.get(0)?,
                root_scope: row.get(1)?,
                revision: row.get(2)?,
            })
        },
    )?;
    if binding.revision < 0 {
        return Err(ManifestError::InvalidBaseline);
    }
    Ok(binding)
}

fn read_records(database: &Connection) -> Result<Vec<ManifestRecord>, ManifestError> {
    let mut statement = database.prepare(
        "SELECT external_id, relative_path, path_sha256, size_bytes,
                modified_before_epoch, modified_secs, modified_nanos, content_sha256, mime_type
         FROM manifest_files LIMIT ?1",
    )?;
    let mut rows = statement.query([(MAX_ENTRIES + 1) as i64])?;
    let mut records = Vec::new();
    while let Some(row) = rows.next()? {
        let mime_type: String = row.get(8)?;
        let modified_at = decode_timestamp(row.get(4)?, row.get(5)?, row.get(6)?)
            .ok_or(ManifestError::InvalidBaseline)?;
        records.push(ManifestRecord {
            external_id: row.get(0)?,
            relative_path: row.get(1)?,
            absolute_path_sha256: row.get(2)?,
            size_bytes: row
                .get::<_, i64>(3)?
                .try_into()
                .map_err(|_| ManifestError::InvalidBaseline)?,
            modified_at,
            content_sha256: row.get(7)?,
            mime_type: supported_mime(&mime_type).ok_or(ManifestError::InvalidBaseline)?,
        });
    }
    validate_records(&records, ManifestError::InvalidBaseline)?;
    Ok(records)
}

fn supported_mime(value: &str) -> Option<&'static str> {
    match value {
        "text/plain" => Some("text/plain"),
        "text/markdown" => Some("text/markdown"),
        "text/csv" => Some("text/csv"),
        _ => None,
    }
}

fn valid_relative_path(value: &str) -> bool {
    value.len() <= MAX_PATH_BYTES && value.split('/').all(permitted_path_component)
}

fn validate_records(records: &[ManifestRecord], error: ManifestError) -> Result<(), ManifestError> {
    if records.len() > MAX_ENTRIES {
        return Err(error);
    }
    let mut ids = HashSet::with_capacity(records.len());
    let mut paths = HashSet::with_capacity(records.len());
    for record in records {
        if record.size_bytes > MAX_FILE_BYTES
            || supported_mime(record.mime_type).is_none()
            || encode_timestamp(record.modified_at).is_none()
            || !valid_relative_path(&record.relative_path)
            || !ids.insert(record.external_id)
            || !paths.insert(record.relative_path.as_str())
        {
            return Err(error);
        }
    }
    Ok(())
}

fn diff(old: &[ManifestRecord], current: &[ManifestRecord]) -> Vec<ManifestChange> {
    let mut by_id = BTreeMap::new();
    for record in old {
        by_id.insert(record.external_id, (Some(record), None));
    }
    for record in current {
        by_id.entry(record.external_id).or_default().1 = Some(record);
    }
    by_id
        .into_values()
        .filter_map(|(before, after)| match (before, after) {
            (None, Some(record)) => Some(ManifestChange::Added(record.clone())),
            (Some(record), None) => Some(ManifestChange::Removed {
                external_id: record.external_id,
                relative_path: record.relative_path.clone(),
            }),
            (Some(before), Some(record)) if before.relative_path != record.relative_path => {
                Some(ManifestChange::Renamed {
                    previous_relative_path: before.relative_path.clone(),
                    record: record.clone(),
                    content_changed: before.content_sha256 != record.content_sha256,
                })
            }
            (Some(before), Some(record)) if before != record => {
                Some(ManifestChange::Modified(record.clone()))
            }
            _ => None,
        })
        .collect()
}

fn encode_timestamp(value: SystemTime) -> Option<(bool, i64, u32)> {
    let (before, duration) = match value.duration_since(UNIX_EPOCH) {
        Ok(duration) => (false, duration),
        Err(error) => (true, error.duration()),
    };
    Some((
        before,
        duration.as_secs().try_into().ok()?,
        duration.subsec_nanos(),
    ))
}

fn decode_timestamp(before: bool, seconds: i64, nanos: u32) -> Option<SystemTime> {
    if nanos >= 1_000_000_000 || (before && seconds == 0 && nanos == 0) {
        return None;
    }
    let duration = Duration::new(seconds.try_into().ok()?, nanos);
    if before {
        UNIX_EPOCH.checked_sub(duration)
    } else {
        UNIX_EPOCH.checked_add(duration)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SkipCounts;

    const SCOPE: Digest = [9; 32];
    const OTHER_SCOPE: Digest = [8; 32];

    fn record(id: u8, path: &str, content: u8) -> ManifestRecord {
        ManifestRecord {
            external_id: [id; 32],
            relative_path: path.into(),
            absolute_path_sha256: [id; 32],
            size_bytes: 3,
            modified_at: UNIX_EPOCH - Duration::from_nanos(51),
            content_sha256: [content; 32],
            mime_type: "text/plain",
        }
    }

    fn snapshot(records: Vec<ManifestRecord>) -> Snapshot {
        Snapshot {
            root_scope: SCOPE,
            records,
            authoritative: true,
            skipped: SkipCounts::default(),
        }
    }

    fn memory_store() -> ManifestStore {
        ManifestStore::for_scope(Connection::open_in_memory().unwrap(), SCOPE).unwrap()
    }

    #[test]
    fn plan_does_not_mutate_and_ack_distinguishes_all_four_change_kinds() {
        let mut store = memory_store();
        let original = vec![
            record(1, "old-name.txt", 5),
            record(2, "modified.txt", 5),
            record(3, "removed.txt", 5),
            record(5, "stays.txt", 5),
        ];
        let plan = store.plan(snapshot(original.clone())).unwrap();
        assert_eq!(plan.changes().len(), 4);
        assert!(plan
            .changes()
            .iter()
            .all(|change| matches!(change, ManifestChange::Added(_))));
        assert_eq!(store.plan(snapshot(original)).unwrap().changes().len(), 4);
        store.acknowledge(plan).unwrap();

        let changed = vec![
            record(5, "stays.txt", 5),
            record(1, "moved/new-name.txt", 6),
            record(2, "modified.txt", 6),
            record(4, "added.txt", 5),
        ];
        let plan = store.plan(snapshot(changed.clone())).unwrap();
        assert!(matches!(
            plan.changes(),
            [
                ManifestChange::Renamed {
                    previous_relative_path,
                    record: moved,
                    content_changed: true,
                },
                ManifestChange::Modified(modified),
                ManifestChange::Removed { relative_path, .. },
                ManifestChange::Added(added),
            ] if previous_relative_path == "old-name.txt"
                && moved.relative_path == "moved/new-name.txt"
                && modified.relative_path == "modified.txt"
                && relative_path == "removed.txt"
                && added.relative_path == "added.txt"
        ));
        store.acknowledge(plan).unwrap();
        assert!(store.plan(snapshot(changed)).unwrap().changes().is_empty());
    }

    #[test]
    fn partial_mismatched_and_invalid_scans_never_advance_baseline() {
        let mut store = memory_store();
        let keep = record(1, "keep.txt", 5);
        let baseline = store.plan(snapshot(vec![keep.clone()])).unwrap();
        store.acknowledge(baseline).unwrap();
        assert_eq!(
            store
                .plan(Snapshot {
                    authoritative: false,
                    ..snapshot(vec![])
                })
                .err(),
            Some(ManifestError::IncompleteSnapshot)
        );
        assert_eq!(
            store
                .plan(Snapshot {
                    root_scope: OTHER_SCOPE,
                    ..snapshot(vec![])
                })
                .err(),
            Some(ManifestError::RootMismatch)
        );
        for invalid in [
            vec![record(1, "../private.txt", 5)],
            vec![record(1, "one.txt", 5), record(1, "two.txt", 5)],
            vec![record(1, "same.txt", 5), record(2, "same.txt", 5)],
        ] {
            assert_eq!(
                store.plan(snapshot(invalid)).err(),
                Some(ManifestError::InvalidSnapshot)
            );
        }
        assert!(store
            .plan(snapshot(vec![keep]))
            .unwrap()
            .changes()
            .is_empty());
    }

    #[test]
    fn stale_or_cross_store_plan_cannot_apply() {
        let mut store = memory_store();
        let mut other_store = memory_store();
        let first = store
            .plan(snapshot(vec![record(1, "initial.txt", 5)]))
            .unwrap();
        let second = store
            .plan(snapshot(vec![record(2, "later.txt", 5)]))
            .unwrap();
        let cross_store = store
            .plan(snapshot(vec![record(1, "initial.txt", 5)]))
            .unwrap();
        assert_eq!(
            other_store.acknowledge(cross_store),
            Err(ManifestError::StalePlan)
        );
        store.acknowledge(first).unwrap();
        assert_eq!(store.acknowledge(second), Err(ManifestError::StalePlan));
    }

    #[test]
    fn database_refuses_unknown_schema() {
        let db = Connection::open_in_memory().unwrap();
        db.execute("CREATE TABLE other (id INTEGER)", []).unwrap();
        assert_eq!(
            ManifestStore::for_scope(db, SCOPE).err(),
            Some(ManifestError::IncompatibleDatabase)
        );
    }

    #[test]
    fn failed_ack_rolls_back_replacement_rows() {
        let mut store = memory_store();
        let before = record(1, "before.txt", 5);
        let first = store.plan(snapshot(vec![before.clone()])).unwrap();
        store.acknowledge(first).unwrap();
        store
            .database
            .execute(
                "UPDATE baseline SET revision = ?1 WHERE singleton = 1",
                [i64::MAX],
            )
            .unwrap();
        let overflowing = store
            .plan(snapshot(vec![record(2, "after.txt", 6)]))
            .unwrap();
        assert_eq!(
            store.acknowledge(overflowing),
            Err(ManifestError::InvalidBaseline)
        );
        assert!(store
            .plan(snapshot(vec![before]))
            .unwrap()
            .changes()
            .is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn reopens_real_local_database_and_keeps_selection_boundaries() {
        use std::fs;
        use std::os::unix::fs::DirBuilderExt;

        // The persisted database is a sibling of the synthetic scanned root,
        // on the same filesystem, and is never considered a scan input.
        let base = std::env::temp_dir().join(format!("uas-manifest-test-{}", std::process::id()));
        fs::DirBuilder::new().mode(0o700).create(&base).unwrap();
        let canonical = fs::canonicalize(&base).unwrap();
        let root_folder = canonical.join("selected");
        fs::create_dir(&root_folder).unwrap();
        let db_path = canonical.join("manifest.sqlite");
        let selected = SelectedRoot::open_after_selection(&root_folder, [4; 32]).unwrap();
        let different = SelectedRoot::open_after_selection(&root_folder, [5; 32]).unwrap();
        fs::write(root_folder.join("one.txt"), "hello").unwrap();
        {
            let mut store =
                ManifestStore::from_connection(Connection::open(&db_path).unwrap(), &selected)
                    .unwrap();
            let plan = store.plan(selected.scan().unwrap()).unwrap();
            assert_eq!(plan.changes().len(), 1);
            store.acknowledge(plan).unwrap();
        }
        {
            let mut reopened =
                ManifestStore::from_connection(Connection::open(&db_path).unwrap(), &selected)
                    .unwrap();
            assert!(reopened
                .plan(selected.scan().unwrap())
                .unwrap()
                .changes()
                .is_empty());
            fs::remove_file(root_folder.join("one.txt")).unwrap();
            let removal = reopened.plan(selected.scan().unwrap()).unwrap();
            assert!(
                matches!(removal.changes(), [ManifestChange::Removed { relative_path, .. }] if relative_path == "one.txt")
            );
        }
        assert_eq!(
            ManifestStore::from_connection(Connection::open(&db_path).unwrap(), &different).err(),
            Some(ManifestError::RootMismatch)
        );
        fs::remove_dir_all(base).unwrap();
    }
}
