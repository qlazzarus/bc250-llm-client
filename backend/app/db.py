import sqlite3
from pathlib import Path

LATEST_SCHEMA_VERSION = 22

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS conversations (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS generation_requests (
 id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 user_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
 assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','cancelled')),
 error_code TEXT,
 error_message TEXT,
 queued_at TEXT NOT NULL,
 started_at TEXT,
 model_id TEXT,
 requested_max_tokens INTEGER,
 estimated_input_tokens INTEGER,
 finish_reason TEXT,
 usage_json TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generation_requests_conversation_status ON generation_requests(conversation_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_requests_one_pending_per_conversation ON generation_requests(conversation_id) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS summary_jobs (
 id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 from_message_id TEXT,
 through_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
 error_message TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_jobs_one_active_per_conversation ON summary_jobs(conversation_id) WHERE status IN ('pending','running');
CREATE TABLE IF NOT EXISTS wiki_memory_jobs (
 generation_id TEXT PRIMARY KEY REFERENCES generation_requests(id) ON DELETE CASCADE,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 status TEXT NOT NULL, action TEXT, document_id INTEGER, title TEXT, source_text TEXT,
 previous_content TEXT, proposed_content TEXT, error_message TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 explicit INTEGER NOT NULL DEFAULT 0, source_message_id TEXT,
 memory_kind TEXT, reason_code TEXT, needs_attention INTEGER, policy_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wiki_memory_conversation ON wiki_memory_jobs(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS schema_migrations (
 version INTEGER PRIMARY KEY,
 applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(path: str) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)
        _apply_conversation_mode_migration(connection)
        connection.execute("UPDATE conversations SET context_policy='recent' WHERE mode='quick' AND context_policy='none'")
        _apply_generation_request_migration(connection)
        _apply_generation_metrics_migration(connection)
        _apply_summary_migration(connection)
        _apply_image_prompt_stateless_migration(connection)
        _apply_image_generation_storage_migration(connection)
        _apply_image_job_status_migration(connection)
        _apply_image_options_migration(connection)
        _apply_prompt_mode_migration(connection)
        _apply_image_model_selection_migration(connection)
        _apply_studio_metadata_migration(connection)
        _apply_image_evaluation_migration(connection)
        _apply_image_preset_migration(connection)
        _apply_title_migration(connection)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(wiki_memory_jobs)")}
        if "explicit" not in columns:
            connection.execute("ALTER TABLE wiki_memory_jobs ADD COLUMN explicit INTEGER NOT NULL DEFAULT 0")
        if "source_message_id" not in columns:
            connection.execute("ALTER TABLE wiki_memory_jobs ADD COLUMN source_message_id TEXT")
        _apply_wiki_memory_metadata_migration(connection)
        _apply_memory_repository_migration(connection)
        _apply_integrated_wiki_migration(connection)
        _apply_memory_namespace_migration(connection)
        _apply_wiki_revision_migration(connection)
        _apply_wiki_alias_migration(connection)
        _apply_wiki_evidence_migration(connection)
        _apply_wiki_message_save_migration(connection)


def _apply_conversation_mode_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversations)")}
    additions = {
        "mode": "TEXT NOT NULL DEFAULT 'quick'",
        "context_policy": "TEXT NOT NULL DEFAULT 'recent'",
        "mode_settings": "TEXT NOT NULL DEFAULT '{}'",
        "response_length": "TEXT NOT NULL DEFAULT 'medium'",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "summary_up_to_message_id": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE conversations ADD COLUMN {name} {definition}")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")


def _apply_generation_request_migration(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS generation_requests (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
            assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','cancelled')),
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_generation_requests_conversation_status ON generation_requests(conversation_id, status)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_requests_one_pending_per_conversation ON generation_requests(conversation_id) WHERE status = 'pending'")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)")


def _apply_generation_metrics_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(generation_requests)")}
    additions = {
        "queued_at": "TEXT",
        "started_at": "TEXT",
        "model_id": "TEXT",
        "requested_max_tokens": "INTEGER",
        "estimated_input_tokens": "INTEGER",
        "finish_reason": "TEXT",
        "usage_json": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE generation_requests ADD COLUMN {name} {definition}")
    connection.execute("UPDATE generation_requests SET queued_at=created_at WHERE queued_at IS NULL")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")


def _apply_summary_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversations)")}
    additions = {
        "summary_status": "TEXT NOT NULL DEFAULT 'idle'",
        "summary_error": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE conversations ADD COLUMN {name} {definition}")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS summary_jobs (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            from_message_id TEXT,
            through_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_jobs_one_active_per_conversation ON summary_jobs(conversation_id) WHERE status IN ('pending','running')")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")


def _apply_image_prompt_stateless_migration(connection: sqlite3.Connection) -> None:
    """Keep old image-prompt records, but never treat them as chat context."""
    connection.execute("UPDATE conversations SET context_policy='none' WHERE mode='image_prompt'")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")


def _apply_image_generation_storage_migration(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS image_drafts (
            source_message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            prompt TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_image_drafts_conversation ON image_drafts(conversation_id, updated_at DESC)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS image_jobs (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            source_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
            draft_revision INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            steps INTEGER NOT NULL,
            cfg_scale REAL NOT NULL,
            payload_fingerprint TEXT NOT NULL,
            client_reference TEXT NOT NULL UNIQUE,
            upstream_job_id TEXT UNIQUE,
            status TEXT NOT NULL CHECK(status IN (
                'submission_pending','submission_unknown','queued','waiting_for_gpu',
                'running','completed','failed','cancelled','interrupted'
            )),
            error_code TEXT,
            error_message TEXT,
            asset_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_image_jobs_conversation_created ON image_jobs(conversation_id, created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_image_jobs_status ON image_jobs(status)")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)")


def _apply_image_job_status_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(image_jobs)")}
    if "cancel_requested_at" not in columns:
        connection.execute("ALTER TABLE image_jobs ADD COLUMN cancel_requested_at TEXT")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)")


def _apply_image_options_migration(connection: sqlite3.Connection) -> None:
    for table in ("image_drafts", "image_jobs"):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "generation_config_json" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN generation_config_json TEXT NOT NULL DEFAULT '{{}}'")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)")


def _apply_prompt_mode_migration(connection: sqlite3.Connection) -> None:
    for table in ("generation_requests", "image_jobs"):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "prompt_mode" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN prompt_mode TEXT")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (9)")


def _apply_image_model_selection_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(generation_requests)")}
    if "image_model_preference" not in columns:
        connection.execute("ALTER TABLE generation_requests ADD COLUMN image_model_preference TEXT")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (10)")


def _apply_studio_metadata_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(image_jobs)")}
    if "studio_metadata_json" not in columns:
        connection.execute("ALTER TABLE image_jobs ADD COLUMN studio_metadata_json TEXT NOT NULL DEFAULT '{}'")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (11)")


def _apply_image_evaluation_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(image_jobs)")}
    for name, definition in {"evaluation_score": "INTEGER", "evaluation_note": "TEXT NOT NULL DEFAULT ''", "evaluation_updated_at": "TEXT"}.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE image_jobs ADD COLUMN {name} {definition}")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (12)")


def _apply_image_preset_migration(connection: sqlite3.Connection) -> None:
    for table in ("image_drafts", "image_jobs"):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "preset_name" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN preset_name TEXT")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (13)")


def _apply_title_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversations)")}
    if "title_pending" not in columns:
        connection.execute("ALTER TABLE conversations ADD COLUMN title_pending INTEGER NOT NULL DEFAULT 0")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (14)")


def _apply_wiki_memory_metadata_migration(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(wiki_memory_jobs)")}
    additions = {
        "memory_kind": "TEXT",
        "reason_code": "TEXT",
        "needs_attention": "INTEGER",
        "policy_version": "INTEGER",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE wiki_memory_jobs ADD COLUMN {name} {definition}")
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (15)")


def _apply_memory_repository_migration(connection: sqlite3.Connection) -> None:
    """Add inert memory metadata tables without importing existing Wiki content."""
    statements = [
        """CREATE TABLE IF NOT EXISTS memory_records (
            id TEXT PRIMARY KEY, scope_key TEXT NOT NULL CHECK(scope_key = 'shared'),
            subject_key TEXT NOT NULL CHECK(length(trim(subject_key)) > 0),
            predicate_key TEXT NOT NULL CHECK(length(trim(predicate_key)) > 0),
            kind TEXT NOT NULL CHECK(kind IN ('fact','preference','plan')),
            value TEXT, normalized_value TEXT, fingerprint TEXT,
            source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
            source_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
            source_start INTEGER, source_end INTEGER, wiki_document_id INTEGER, wiki_revision TEXT,
            state TEXT NOT NULL CHECK(state IN ('candidate','active','superseded','quarantined','redacted')),
            supersedes_id TEXT REFERENCES memory_records(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK((state = 'redacted' AND value IS NULL AND normalized_value IS NULL AND fingerprint IS NULL
                   AND source_start IS NULL AND source_end IS NULL)
               OR (state <> 'redacted' AND value IS NOT NULL AND normalized_value IS NOT NULL AND fingerprint IS NOT NULL
                   AND source_conversation_id IS NOT NULL AND source_message_id IS NOT NULL
                   AND source_start IS NOT NULL AND source_start >= 0 AND source_end IS NOT NULL AND source_end > source_start)),
            CHECK(state <> 'active' OR (wiki_document_id IS NOT NULL AND wiki_revision IS NOT NULL)))""",
        "CREATE UNIQUE INDEX IF NOT EXISTS memory_one_active_slot ON memory_records(scope_key,subject_key,predicate_key) WHERE state = 'active'",
        "CREATE INDEX IF NOT EXISTS memory_fingerprint_lookup ON memory_records(scope_key,fingerprint) WHERE fingerprint IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS memory_source_lookup ON memory_records(source_message_id)",
        "CREATE INDEX IF NOT EXISTS memory_document_lookup ON memory_records(wiki_document_id)",
        """CREATE TABLE IF NOT EXISTS memory_operations (
            id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE RESTRICT,
            scope_key TEXT NOT NULL, subject_key TEXT NOT NULL, predicate_key TEXT NOT NULL,
            operation_type TEXT NOT NULL CHECK(operation_type IN ('create','supersede')),
            request_hash TEXT NOT NULL, expected_revision TEXT, wiki_document_id INTEGER,
            state TEXT NOT NULL CHECK(state IN ('claimed','prepared','sending','unknown','confirmed','applied','rejected','cancelled')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS memory_one_open_operation ON memory_operations(scope_key,subject_key,predicate_key) WHERE state IN ('claimed','prepared','sending','unknown','confirmed')",
        "CREATE INDEX IF NOT EXISTS memory_operation_record ON memory_operations(record_id)",
        """CREATE TABLE IF NOT EXISTS memory_document_quarantine (
            wiki_document_id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL CHECK(reason IN ('owned','source_deleted','external_change','unknown_write')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    ]
    connection.execute("SAVEPOINT memory_repository_migration")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (16)")
        connection.execute("RELEASE SAVEPOINT memory_repository_migration")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT memory_repository_migration")
        connection.execute("RELEASE SAVEPOINT memory_repository_migration")
        raise


def _apply_integrated_wiki_migration(connection: sqlite3.Connection) -> None:
    """Add an empty local Wiki and its transactional FTS index to the client DB."""
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version=17").fetchone():
        return
    statements = [
        """CREATE TABLE wiki_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
            content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 100000),
            category TEXT NOT NULL DEFAULT 'general' CHECK(length(category) <= 80),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision) = 'integer' AND revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE VIRTUAL TABLE wiki_documents_fts USING fts5(title, content, tokenize='unicode61')",
        """CREATE TRIGGER wiki_documents_ai AFTER INSERT ON wiki_documents BEGIN
            INSERT INTO wiki_documents_fts(rowid,title,content) VALUES(new.id,new.title,new.content);
        END""",
        """CREATE TRIGGER wiki_documents_au AFTER UPDATE ON wiki_documents BEGIN
            DELETE FROM wiki_documents_fts WHERE rowid=old.id;
            INSERT INTO wiki_documents_fts(rowid,title,content) VALUES(new.id,new.title,new.content);
        END""",
        """CREATE TRIGGER wiki_documents_ad AFTER DELETE ON wiki_documents BEGIN
            DELETE FROM wiki_documents_fts WHERE rowid=old.id;
        END""",
    ]
    connection.execute("SAVEPOINT integrated_wiki_migration")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (17)")
        connection.execute("RELEASE SAVEPOINT integrated_wiki_migration")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT integrated_wiki_migration")
        connection.execute("RELEASE SAVEPOINT integrated_wiki_migration")
        raise


def _apply_memory_namespace_migration(connection: sqlite3.Connection) -> None:
    """Preserve old Wiki references while separating integrated document IDs."""
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version=18").fetchone():
        return
    statements = [
        "ALTER TABLE wiki_memory_jobs ADD COLUMN store_id TEXT NOT NULL DEFAULT 'legacy_external' CHECK(store_id IN ('legacy_external','integrated'))",
        "ALTER TABLE memory_records ADD COLUMN store_id TEXT NOT NULL DEFAULT 'legacy_external' CHECK(store_id IN ('legacy_external','integrated'))",
        "ALTER TABLE memory_operations ADD COLUMN store_id TEXT NOT NULL DEFAULT 'legacy_external' CHECK(store_id IN ('legacy_external','integrated'))",
        "ALTER TABLE memory_document_quarantine RENAME TO memory_document_quarantine_v16",
        """CREATE TABLE memory_document_quarantine (
            store_id TEXT NOT NULL CHECK(store_id IN ('legacy_external','integrated')),
            wiki_document_id INTEGER NOT NULL,
            reason TEXT NOT NULL CHECK(reason IN ('owned','source_deleted','external_change','unknown_write')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(store_id,wiki_document_id))""",
        """INSERT INTO memory_document_quarantine(store_id,wiki_document_id,reason,created_at,updated_at)
            SELECT 'legacy_external',wiki_document_id,reason,created_at,updated_at FROM memory_document_quarantine_v16""",
        "DROP TABLE memory_document_quarantine_v16",
        "DROP INDEX memory_one_active_slot",
        "CREATE UNIQUE INDEX memory_one_active_slot ON memory_records(store_id,scope_key,subject_key,predicate_key) WHERE state='active'",
        "DROP INDEX memory_fingerprint_lookup",
        "CREATE INDEX memory_fingerprint_lookup ON memory_records(store_id,scope_key,fingerprint) WHERE fingerprint IS NOT NULL",
        "DROP INDEX memory_document_lookup",
        "CREATE INDEX memory_document_lookup ON memory_records(store_id,wiki_document_id)",
        "DROP INDEX memory_one_open_operation",
        "CREATE UNIQUE INDEX memory_one_open_operation ON memory_operations(store_id,scope_key,subject_key,predicate_key) WHERE state IN ('claimed','prepared','sending','unknown','confirmed')",
        """CREATE TRIGGER memory_operation_store_insert BEFORE INSERT ON memory_operations
           WHEN NOT EXISTS (SELECT 1 FROM memory_records r WHERE r.id=NEW.record_id AND r.store_id=NEW.store_id)
           BEGIN SELECT RAISE(ABORT,'operation store mismatch'); END""",
        """CREATE TRIGGER memory_operation_store_update BEFORE UPDATE OF store_id,record_id ON memory_operations
           WHEN NOT EXISTS (SELECT 1 FROM memory_records r WHERE r.id=NEW.record_id AND r.store_id=NEW.store_id)
           BEGIN SELECT RAISE(ABORT,'operation store mismatch'); END""",
        """CREATE TRIGGER memory_record_store_immutable BEFORE UPDATE OF store_id ON memory_records
           WHEN OLD.store_id<>NEW.store_id BEGIN SELECT RAISE(ABORT,'record store immutable'); END""",
        """CREATE TRIGGER memory_supersedes_store_insert BEFORE INSERT ON memory_records
           WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM memory_records r WHERE r.id=NEW.supersedes_id AND r.store_id=NEW.store_id)
           BEGIN SELECT RAISE(ABORT,'supersedes store mismatch'); END""",
        """CREATE TRIGGER memory_supersedes_store_update BEFORE UPDATE OF supersedes_id ON memory_records
           WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM memory_records r WHERE r.id=NEW.supersedes_id AND r.store_id=NEW.store_id)
           BEGIN SELECT RAISE(ABORT,'supersedes store mismatch'); END""",
    ]
    connection.execute('SAVEPOINT memory_namespace_migration')
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute('INSERT INTO schema_migrations(version) VALUES (18)')
        connection.execute('RELEASE SAVEPOINT memory_namespace_migration')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT memory_namespace_migration')
        connection.execute('RELEASE SAVEPOINT memory_namespace_migration')
        raise


def _apply_wiki_revision_migration(connection: sqlite3.Connection) -> None:
    """Keep at most 100 snapshots per integrated document, deleting with it."""
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=19').fetchone():
        return
    statements = [
        """CREATE TABLE wiki_document_revisions (
            document_id INTEGER NOT NULL REFERENCES wiki_documents(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision > 0),
            title TEXT NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(document_id, revision))""",
        """INSERT INTO wiki_document_revisions(document_id,revision,title,content,category,created_at)
           SELECT id,revision,title,content,category,updated_at FROM wiki_documents""",
        """CREATE TRIGGER wiki_revision_ai AFTER INSERT ON wiki_documents BEGIN
            INSERT INTO wiki_document_revisions VALUES(new.id,new.revision,new.title,new.content,new.category,new.updated_at);
        END""",
        """CREATE TRIGGER wiki_revision_au AFTER UPDATE ON wiki_documents BEGIN
            SELECT CASE WHEN new.revision <> old.revision+1 THEN RAISE(ABORT,'revision must increase by one') END;
            INSERT INTO wiki_document_revisions VALUES(new.id,new.revision,new.title,new.content,new.category,new.updated_at);
            DELETE FROM wiki_document_revisions WHERE document_id=new.id AND revision NOT IN
              (SELECT revision FROM wiki_document_revisions WHERE document_id=new.id ORDER BY revision DESC LIMIT 100);
        END""",
    ]
    connection.execute('SAVEPOINT wiki_revision_migration')
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute('INSERT INTO schema_migrations(version) VALUES (19)')
        connection.execute('RELEASE SAVEPOINT wiki_revision_migration')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT wiki_revision_migration')
        connection.execute('RELEASE SAVEPOINT wiki_revision_migration')
        raise


def _apply_wiki_alias_migration(connection: sqlite3.Connection) -> None:
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=20').fetchone():
        return
    connection.execute('SAVEPOINT wiki_alias_migration')
    try:
        connection.execute('''CREATE TABLE wiki_document_aliases (
            document_id INTEGER NOT NULL REFERENCES wiki_documents(id) ON DELETE CASCADE,
            alias_original TEXT NOT NULL, alias_key TEXT NOT NULL,
            normalization_version INTEGER NOT NULL CHECK(normalization_version=1),
            PRIMARY KEY(document_id, alias_key))''')
        connection.execute('CREATE INDEX wiki_alias_lookup ON wiki_document_aliases(normalization_version,alias_key,document_id)')
        connection.execute('INSERT INTO schema_migrations(version) VALUES (20)')
        connection.execute('RELEASE SAVEPOINT wiki_alias_migration')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT wiki_alias_migration')
        connection.execute('RELEASE SAVEPOINT wiki_alias_migration')
        raise


def _apply_wiki_evidence_migration(connection: sqlite3.Connection) -> None:
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=21').fetchone():
        return
    connection.execute('SAVEPOINT wiki_evidence_migration')
    try:
        connection.execute('''CREATE TABLE wiki_message_evidence (
            message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            evidence_json TEXT NOT NULL)''')
        connection.execute('INSERT INTO schema_migrations(version) VALUES (21)')
        connection.execute('RELEASE SAVEPOINT wiki_evidence_migration')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT wiki_evidence_migration')
        connection.execute('RELEASE SAVEPOINT wiki_evidence_migration')
        raise


def _apply_wiki_message_save_migration(connection: sqlite3.Connection) -> None:
    if connection.execute('SELECT 1 FROM schema_migrations WHERE version=22').fetchone():
        return
    connection.execute('SAVEPOINT wiki_message_save_migration')
    try:
        connection.execute("""CREATE TABLE wiki_message_save_receipts (
            request_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL,
            document_id INTEGER NOT NULL, revision INTEGER NOT NULL, action TEXT NOT NULL,
            source_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE wiki_document_sources (
            document_id INTEGER NOT NULL REFERENCES wiki_documents(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL, source_text TEXT NOT NULL,
            source_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
            PRIMARY KEY(document_id, revision))""")
        connection.execute('INSERT INTO schema_migrations(version) VALUES (22)')
        connection.execute('RELEASE SAVEPOINT wiki_message_save_migration')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT wiki_message_save_migration')
        connection.execute('RELEASE SAVEPOINT wiki_message_save_migration')
        raise
