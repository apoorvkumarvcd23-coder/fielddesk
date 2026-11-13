"""Immutable knowledge versions and fenced, durable indexing jobs."""
from dataclasses import asdict
import hashlib
import json
import time
from uuid import uuid4

from embeddings import CHUNKING_VERSION, HashEmbedder, validate_vectors


def identifier(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def chunks_for(version_id, body):
    """Offsets are Python Unicode character offsets, not bytes or token counts."""
    chunks = []
    start = 0
    while start < len(body):
        end = min(start + 600, len(body))
        ordinal = len(chunks)
        chunks.append({'id': identifier(version_id, ordinal), 'ordinal': ordinal,
                       'start_offset': start, 'end_offset': end, 'text': body[start:end]})
        if end == len(body):
            break
        start = end - 80
    return chunks


class KnowledgeStore:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock

    def register_space(self, space):
        if space.chunking_version != CHUNKING_VERSION or not 1 <= space.dimensions <= 4096:
            raise ValueError('Unsupported embedding space')
        with self.db.connect() as conn:
            self.db.run(conn, 'INSERT INTO embedding_spaces VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                        (space.id, space.backend, space.model, space.digest, space.dimensions, space.chunking_version, self.clock()))
            row = self.db.run(conn, 'SELECT * FROM embedding_spaces WHERE id=?', (space.id,)).fetchone()
            if any(row[key] != value for key, value in asdict(space).items()):
                raise ValueError('Embedding space metadata mismatch')
        return space.id

    def spaces(self):
        with self.db.connect() as conn:
            return [dict(row) for row in self.db.run(conn, 'SELECT * FROM embedding_spaces ORDER BY id').fetchall()]

    def ingest(self, source_key, title, body, space_id=None, actor_id=None, legacy_api=False):
        if not body.strip() or len(body) > 20000 or not title.strip() or len(title) > 150 or '\0' in body or '\0' in title:
            raise ValueError('Invalid knowledge text')
        space_id = space_id or self.register_space(HashEmbedder.space)
        document_id = identifier(source_key)
        content_hash = identifier(title, body)
        version_id = identifier(document_id, content_hash, CHUNKING_VERSION)
        job_id = identifier(version_id, space_id)
        now = self.clock()
        with self.db.connect() as conn:
            space = self.db.run(conn, 'SELECT * FROM embedding_spaces WHERE id=?', (space_id,)).fetchone()
            if not space or space['chunking_version'] != CHUNKING_VERSION:
                raise ValueError('Unknown embedding space; register it with the worker first')
            if actor_id:
                suffix = ' FOR UPDATE' if self.db.postgres else ''
                actor = self.db.run(conn, 'SELECT role,enabled FROM rag_identities WHERE id=?' + suffix, (actor_id,)).fetchone()
                if not actor or not actor['enabled'] or actor['role'] not in {'operator','admin'}:
                    raise PermissionError('Enabled operator required')
            created = self.db.run(conn, 'INSERT INTO knowledge_documents VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',
                        (document_id, source_key, now)).rowcount
            # Serialize different version submissions for the same source on PostgreSQL.
            suffix = ' FOR UPDATE' if self.db.postgres else ''
            self.db.run(conn, 'SELECT id FROM knowledge_documents WHERE id=?' + suffix, (document_id,)).fetchone()
            if legacy_api and self.db.run(conn, 'SELECT document_id FROM document_permissions WHERE document_id=?', (document_id,)).fetchone():
                raise PermissionError('Identity-managed sources require identity authorization')
            if actor_id:
                if created:
                    self.db.run(conn, 'INSERT INTO document_permissions VALUES (?,1)', (document_id,))
                    self.db.run(conn, 'INSERT INTO document_grants VALUES (?,?,1)', (document_id, actor_id))
                else:
                    suffix = ' FOR SHARE OF g,p' if self.db.postgres else ''
                    allowed = self.db.run(conn, '''SELECT g.enabled FROM document_grants g JOIN document_permissions p ON p.document_id=g.document_id
                        WHERE g.document_id=? AND g.identity_id=? AND g.enabled=1 AND p.enabled=1''' + suffix, (document_id, actor_id)).fetchone()
                    if not allowed:
                        raise PermissionError('Source access not granted')
            inserted = self.db.run(conn, '''INSERT INTO document_versions VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(id) DO NOTHING''',
                (version_id, document_id, title, body, content_hash, CHUNKING_VERSION, now)).rowcount
            self.db.run(conn, '''INSERT INTO index_jobs
                (id,version_id,space_id,status,available_at,created_at) VALUES (?,?,?,'pending',?,?)
                ON CONFLICT(version_id,space_id) DO NOTHING''', (job_id, version_id, space_id, now, now))
            self.db.run(conn, '''INSERT INTO index_targets(document_id,space_id,desired_version_id)
                VALUES (?,?,?) ON CONFLICT(document_id,space_id) DO NOTHING''', (document_id, space_id, version_id))
            if inserted:
                self.db.run(conn, 'UPDATE index_targets SET desired_version_id=? WHERE document_id=? AND space_id=?',
                            (version_id, document_id, space_id))
        # Exact replays return the same job; older replays do not roll back a newer target.
        return self.job(job_id)

    def job(self, job_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, '''SELECT j.id,j.version_id,j.space_id,j.status,j.attempts,j.max_attempts,
                j.available_at,j.lease_until,j.last_error,v.document_id,s.model,s.digest,s.dimensions,
                (SELECT count(*) FROM chunk_embeddings ce JOIN document_chunks dc ON dc.id=ce.chunk_id
                 WHERE dc.version_id=j.version_id AND ce.space_id=j.space_id) AS indexed_chunks
                FROM index_jobs j JOIN document_versions v ON v.id=j.version_id
                JOIN embedding_spaces s ON s.id=j.space_id WHERE j.id=?''', (job_id,)).fetchone()
            return dict(row) if row else None

    def document(self, document_id):
        with self.db.connect() as conn:
            row = self.db.run(conn, 'SELECT * FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['versions'] = [dict(v) for v in self.db.run(conn,
                'SELECT id,title,content_hash,chunking_version,created_at FROM document_versions WHERE document_id=? ORDER BY created_at,id',
                (document_id,)).fetchall()]
            result['indexes'] = [dict(v) for v in self.db.run(conn,
                'SELECT space_id,desired_version_id,ready_version_id FROM index_targets WHERE document_id=? ORDER BY space_id',
                (document_id,)).fetchall()]
            return result

    def ready_chunks(self, version_id, space_id):
        with self.db.connect() as conn:
            job = self.db.run(conn, 'SELECT status FROM index_jobs WHERE version_id=? AND space_id=?',
                              (version_id, space_id)).fetchone()
            if not job or job['status'] != 'ready':
                return None
            return [dict(row) for row in self.db.run(conn, '''SELECT dc.id,dc.ordinal,dc.start_offset,dc.end_offset,dc.text
                FROM document_chunks dc JOIN chunk_embeddings ce ON ce.chunk_id=dc.id
                WHERE dc.version_id=? AND ce.space_id=? ORDER BY dc.ordinal''', (version_id, space_id)).fetchall()]

    def claim(self, space_id, lease_seconds=300):
        if not 1 <= lease_seconds <= 3600:
            raise ValueError('Invalid lease duration')
        now = self.clock()
        with self.db.connect() as conn:
            self.db.run(conn, '''UPDATE index_jobs SET status='failed',lease_token=NULL,lease_until=NULL,last_error='lease_expired'
                WHERE space_id=? AND status='running' AND lease_until<=? AND attempts>=max_attempts''', (space_id, now))
            suffix = ' FOR UPDATE SKIP LOCKED' if self.db.postgres else ''
            row = self.db.run(conn, '''SELECT * FROM index_jobs WHERE space_id=? AND attempts<max_attempts
                AND ((status='pending' AND available_at<=?) OR (status='running' AND lease_until<=?))
                ORDER BY created_at,id LIMIT 1''' + suffix, (space_id, now, now)).fetchone()
            if not row:
                return None
            token = str(uuid4())
            self.db.run(conn, '''UPDATE index_jobs SET status='running',attempts=attempts+1,
                lease_token=?,lease_until=? WHERE id=?''', (token, now + lease_seconds, row['id']))
            version = self.db.run(conn, 'SELECT * FROM document_versions WHERE id=?', (row['version_id'],)).fetchone()
            return {**dict(row), 'lease_token': token, 'attempts': row['attempts'] + 1, 'version': dict(version)}

    def publish(self, job, space, chunks, vectors):
        validate_vectors(vectors, len(chunks), space.dimensions)
        if space.id != job['space_id'] or chunks != chunks_for(job['version_id'], job['version']['body']):
            raise ValueError('Index content/space mismatch')
        with self.db.connect() as conn:
            suffix = ' FOR UPDATE' if self.db.postgres else ''
            current = self.db.run(conn, 'SELECT * FROM index_jobs WHERE id=?' + suffix, (job['id'],)).fetchone()
            if not self._owns(current, job):
                return False
            for chunk, vector in zip(chunks, vectors):
                self.db.run(conn, '''INSERT INTO document_chunks VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
                    (chunk['id'], job['version_id'], chunk['ordinal'], chunk['start_offset'], chunk['end_offset'], chunk['text']))
                self.db.run(conn, '''INSERT INTO chunk_embeddings VALUES (?,?,?,?)
                    ON CONFLICT(chunk_id,space_id) DO NOTHING''', (chunk['id'], space.id, space.dimensions, json.dumps(vector)))
            self.db.run(conn, '''UPDATE index_targets SET ready_version_id=?
                WHERE document_id=? AND space_id=? AND desired_version_id=?''',
                (job['version_id'], job['version']['document_id'], space.id, job['version_id']))
            self.db.run(conn, "UPDATE index_jobs SET status='ready',lease_token=NULL,lease_until=NULL,last_error=NULL WHERE id=?", (job['id'],))
        return True

    def fail(self, job, error_code='embedding_failed'):
        if error_code not in {'embedding_failed', 'invalid_embedding', 'model_unavailable', 'model_not_installed', 'model_changed'}:
            error_code = 'embedding_failed'
        with self.db.connect() as conn:
            suffix = ' FOR UPDATE' if self.db.postgres else ''
            current = self.db.run(conn, 'SELECT * FROM index_jobs WHERE id=?' + suffix, (job['id'],)).fetchone()
            if not self._owns(current, job):
                return False
            state = 'pending' if current['attempts'] < current['max_attempts'] else 'failed'
            self.db.run(conn, '''UPDATE index_jobs SET status=?,available_at=?,lease_token=NULL,lease_until=NULL,last_error=? WHERE id=?''',
                        (state, self.clock() + 2 ** current['attempts'], error_code, job['id']))
        return True

    def _owns(self, current, job):
        return bool(current and current['status'] == 'running' and current['lease_token'] == job['lease_token']
                    and current['lease_until'] > self.clock())


def run_once(store, embedder, lease_seconds=300):
    store.register_space(embedder.space)
    job = store.claim(embedder.space.id, lease_seconds)
    if not job:
        return None
    try:
        chunks = chunks_for(job['version_id'], job['version']['body'])
        vectors = []
        # Model calls occur outside the write transaction and never block API ingestion.
        for start in range(0, len(chunks), 8):
            batch = chunks[start:start + 8]
            values = embedder.embed([chunk['text'] for chunk in batch])
            validate_vectors(values, len(batch), embedder.space.dimensions)
            vectors.extend(values)
        published = store.publish(job, embedder.space, chunks, vectors)
        return {'job_id': job['id'], 'outcome': 'ready' if published else 'lease_lost'}
    except Exception as failure:
        from embeddings import EmbeddingError
        code = str(failure) if isinstance(failure, EmbeddingError) else 'embedding_failed'
        recorded = store.fail(job, code)
        return {'job_id': job['id'], 'outcome': 'retry_or_failed' if recorded else 'lease_lost'}
