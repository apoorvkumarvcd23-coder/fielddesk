"""Transactional persistence and checked migrations for SQLite and PostgreSQL."""
import hashlib
import sqlite3
import time
from contextlib import contextmanager


class Database:
    def __init__(self, url):
        self.url = url
        self.postgres = url.startswith(('postgresql://', 'postgres://'))

    @contextmanager
    def connect(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row)
        else:
            conn = sqlite3.connect(self.url, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run(self, conn, sql, params=()):
        return conn.execute(sql.replace('?', '%s') if self.postgres else sql, params)

    def initialize(self):
        with self.connect() as conn:
            if self.postgres:
                # Serialize first startup/migration across API and worker processes.
                conn.execute('SELECT pg_advisory_xact_lock(72851002)')
                conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
            conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                         '(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at DOUBLE PRECISION NOT NULL)')
            for version, statements in migrations(self.postgres):
                checksum = hashlib.sha256('\n'.join(statements).encode()).hexdigest()
                previous = self.run(conn, 'SELECT checksum FROM schema_migrations WHERE version=?', (version,)).fetchone()
                if previous:
                    if previous['checksum'] != checksum:
                        raise RuntimeError(f'Migration {version} checksum mismatch')
                    continue
                for statement in statements:
                    conn.execute(statement)
                self.run(conn, 'INSERT INTO schema_migrations VALUES (?,?,?)', (version, checksum, time.time()))


def migrations(postgres):
    baseline_vector = 'vector(256)' if postgres else 'TEXT'
    learned_vector = 'vector' if postgres else 'TEXT'
    vector_length = 'vector_dims(embedding)' if postgres else 'json_array_length(embedding)'
    return [
        (1, [
            f'CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, embedding {baseline_vector} NOT NULL)',
            'CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, ticket_id TEXT)',
            'CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS traces (id TEXT PRIMARY KEY, kind TEXT NOT NULL, elapsed_ms DOUBLE PRECISION NOT NULL, outcome TEXT NOT NULL)',
        ]),
        (2, [
            'CREATE TABLE knowledge_documents (id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, created_at DOUBLE PRECISION NOT NULL)',
            '''CREATE TABLE document_versions (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
                title TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT NOT NULL,
                chunking_version TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
                UNIQUE(document_id, content_hash, chunking_version), UNIQUE(document_id, id))''',
            '''CREATE TABLE embedding_spaces (
                id TEXT PRIMARY KEY, backend TEXT NOT NULL, model TEXT NOT NULL, digest TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions > 0 AND dimensions <= 4096),
                chunking_version TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, UNIQUE(id, dimensions))''',
            '''CREATE TABLE document_chunks (
                id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES document_versions(id),
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0), start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
                end_offset INTEGER NOT NULL, text TEXT NOT NULL,
                CHECK(end_offset > start_offset AND length(text) = end_offset - start_offset),
                UNIQUE(version_id, ordinal))''',
            f'''CREATE TABLE chunk_embeddings (
                chunk_id TEXT NOT NULL REFERENCES document_chunks(id), space_id TEXT NOT NULL,
                dimensions INTEGER NOT NULL, embedding {learned_vector} NOT NULL,
                PRIMARY KEY(chunk_id, space_id), FOREIGN KEY(space_id, dimensions) REFERENCES embedding_spaces(id, dimensions),
                CHECK({vector_length} = dimensions))''',
            '''CREATE TABLE index_targets (
                document_id TEXT NOT NULL REFERENCES knowledge_documents(id), space_id TEXT NOT NULL REFERENCES embedding_spaces(id),
                desired_version_id TEXT NOT NULL, ready_version_id TEXT,
                PRIMARY KEY(document_id, space_id),
                FOREIGN KEY(document_id, desired_version_id) REFERENCES document_versions(document_id, id),
                FOREIGN KEY(document_id, ready_version_id) REFERENCES document_versions(document_id, id))''',
            '''CREATE TABLE index_jobs (
                id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES document_versions(id),
                space_id TEXT NOT NULL REFERENCES embedding_spaces(id),
                status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'ready', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10),
                available_at DOUBLE PRECISION NOT NULL, lease_until DOUBLE PRECISION, lease_token TEXT,
                last_error TEXT, created_at DOUBLE PRECISION NOT NULL,
                UNIQUE(version_id, space_id))''',
            'CREATE INDEX index_jobs_claim_idx ON index_jobs(space_id, status, available_at)',
        ]),
        (3, [
            '''CREATE TABLE rag_identities (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK(role IN ('user','operator','admin')),
                token_hash TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))''',
            '''CREATE TABLE document_permissions (
                document_id TEXT PRIMARY KEY REFERENCES knowledge_documents(id),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))''',
            '''CREATE TABLE document_grants (
                document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
                identity_id TEXT NOT NULL REFERENCES rag_identities(id),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                PRIMARY KEY(document_id, identity_id))''',
            '''CREATE TABLE rag_traces (
                id TEXT PRIMARY KEY, identity_id TEXT NOT NULL REFERENCES rag_identities(id),
                space_id TEXT NOT NULL, outcome TEXT NOT NULL,
                elapsed_ms DOUBLE PRECISION NOT NULL, metadata TEXT NOT NULL)''',
        ]),
        (4, [
            '''CREATE TABLE rag_reviews (
                id TEXT PRIMARY KEY, trace_id TEXT NOT NULL UNIQUE REFERENCES rag_traces(id),
                requester_id TEXT NOT NULL REFERENCES rag_identities(id), question TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','in_review','resolved')),
                revision INTEGER NOT NULL DEFAULT 0, assigned_to TEXT REFERENCES rag_identities(id),
                resolution TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL, deleted_at DOUBLE PRECISION)''',
            '''CREATE TABLE rag_ticket_links (
                action_id TEXT PRIMARY KEY REFERENCES actions(id),
                review_id TEXT NOT NULL UNIQUE REFERENCES rag_reviews(id),
                requester_id TEXT NOT NULL REFERENCES rag_identities(id),
                approved_by TEXT REFERENCES rag_identities(id))''',
            '''CREATE TABLE rag_review_events (
                id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES rag_reviews(id),
                actor_id TEXT NOT NULL REFERENCES rag_identities(id),
                event TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)''',
            'CREATE INDEX rag_reviews_queue_idx ON rag_reviews(state,expires_at,created_at)',
        ]),
    ]
