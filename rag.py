"""Identity-bound retrieval with database permission filters and citation rechecks."""
import hashlib
import json
import math
import re
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from embeddings import EmbeddingError, validate_vectors

EVIDENCE_SQL = '''FROM document_chunks c
    JOIN document_versions v ON v.id=c.version_id
    JOIN index_targets t ON t.document_id=v.document_id AND t.ready_version_id=v.id
    JOIN chunk_embeddings e ON e.chunk_id=c.id AND e.space_id=t.space_id
    JOIN document_permissions p ON p.document_id=v.document_id AND p.enabled=1
    JOIN document_grants g ON g.document_id=v.document_id AND g.enabled=1
    JOIN rag_identities i ON i.id=g.identity_id AND i.enabled=1
    WHERE i.id=? AND t.space_id=?'''
FIELDS = 'c.id,c.version_id,v.document_id,v.title,c.text,c.start_offset,c.end_offset'


class RagRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)


class Grant(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


def add_identity(db, name, role, token):
    if role not in {'user', 'operator', 'admin'} or len(token) < 24:
        raise ValueError('Invalid identity/credential')
    identity_id = hashlib.sha256(('fielddesk:' + name).encode()).hexdigest()
    with db.connect() as conn:
        db.run(conn, 'INSERT INTO rag_identities VALUES (?,?,?,?,1) ON CONFLICT(id) DO NOTHING',
               (identity_id, name, role, hashlib.sha256(token.encode()).hexdigest()))
    return identity_id


def set_grant(db, document_id, identity_id, enabled):
    with db.connect() as conn:
        if not db.run(conn, 'SELECT id FROM knowledge_documents WHERE id=?', (document_id,)).fetchone():
            raise HTTPException(404, 'Unknown document')
        if not db.run(conn, 'SELECT id FROM rag_identities WHERE id=?', (identity_id,)).fetchone():
            raise HTTPException(404, 'Unknown identity')
        db.run(conn, 'INSERT INTO document_permissions VALUES (?,1) ON CONFLICT(document_id) DO NOTHING',
               (document_id,))
        db.run(conn, '''INSERT INTO document_grants VALUES (?,?,?)
            ON CONFLICT(document_id,identity_id) DO UPDATE SET enabled=excluded.enabled''',
               (document_id, identity_id, int(enabled)))


class RagService:
    def __init__(self, db, embedder=None, generator=None, threshold=0.35):
        self.db, self.embedder, self.generator, self.threshold = db, embedder, generator, threshold

    def authenticate(self, authorization):
        if not authorization.startswith('Bearer ') or len(authorization) > 512:
            raise HTTPException(401, 'Identity bearer credential required')
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with self.db.connect() as conn:
            row = self.db.run(conn, 'SELECT id,name,role FROM rag_identities WHERE token_hash=? AND enabled=1',
                              (digest,)).fetchone()
        if not row:
            raise HTTPException(401, 'Invalid or disabled identity')
        return dict(row)

    def permitted(self, conn, actor, space_id, chunk_id, lock=False):
        suffix = ' FOR SHARE OF t,p,g,i' if lock and self.db.postgres else ''
        return self.db.run(conn, f'SELECT {FIELDS} {EVIDENCE_SQL} AND c.id=?' + suffix,
                           (actor, space_id, chunk_id)).fetchone()

    def retrieve(self, actor, question):
        vector = self.embedder.embed_query(question)
        validate_vectors([vector], 1, self.embedder.space.dimensions)
        if not any(vector):
            return []
        with self.db.connect() as conn:
            params = (actor, self.embedder.space.id)
            if self.db.postgres:
                rows = self.db.run(conn, f'''SELECT {FIELDS},1-(e.embedding <=> ?::vector) AS score
                    {EVIDENCE_SQL} ORDER BY e.embedding <=> ?::vector,c.id LIMIT 3''',
                    (json.dumps(vector), *params, json.dumps(vector))).fetchall()
                rows = [dict(row) for row in rows]
            else:
                rows = [dict(r) for r in self.db.run(conn,
                    f'SELECT {FIELDS},e.embedding {EVIDENCE_SQL}', params).fetchall()]
                norm = math.sqrt(sum(v*v for v in vector))
                for row in rows:
                    stored = json.loads(row.pop('embedding'))
                    divisor = norm * math.sqrt(sum(v*v for v in stored))
                    row['score'] = sum(a*b for a,b in zip(vector, stored)) / divisor if divisor else 0
                rows.sort(key=lambda r: (-r['score'], r['id']))
                rows = rows[:3]
        return [r for r in rows if r['score'] is not None and r['score'] >= self.threshold]

    def answer(self, actor, question):
        started = time.perf_counter()
        trace_id, outcome, citations, answer, metadata = str(uuid4()), 'unsupported', [], '', {}
        space_id = self.embedder.space.id if self.embedder else 'unconfigured'
        try:
            if not self.embedder or not self.generator:
                raise EmbeddingError('model_unavailable')
            if re.search(r'\b(human|operator|ignore instructions|reveal secret)\b', question, re.I):
                rows = []
            else:
                rows = self.retrieve(actor['id'], question)
            # Close the retrieval -> prompt permission/version race before generation.
            with self.db.connect() as conn:
                rows = [r for r in rows if self.permitted(conn, actor['id'], space_id, r['id'])]
            if rows:
                result, metadata = self.generator.generate(question, rows)
                if not isinstance(result, dict) or set(result) != {'handoff', 'answer', 'citations'}:
                    raise EmbeddingError('invalid_generation')
                if type(result['handoff']) is not bool:
                    raise EmbeddingError('invalid_generation')
                if result['handoff']:
                    outcome = 'unsupported'
                else:
                    if not isinstance(result['answer'], str) or not 1 <= len(result['answer'].strip()) <= 1200:
                        raise EmbeddingError('invalid_generation')
                    proposed = result['citations']
                    if not isinstance(proposed, list) or not 1 <= len(proposed) <= 3:
                        raise EmbeddingError('invalid_citations')
                    by_id = {r['id']: r for r in rows}
                    seen = set()
                    with self.db.connect() as conn:
                        for cite in proposed:
                            if not isinstance(cite, dict) or set(cite) != {'chunk_id', 'quote'}:
                                raise EmbeddingError('invalid_citations')
                            cid, quote = cite['chunk_id'], cite['quote']
                            if not isinstance(cid, str) or cid not in by_id or cid in seen:
                                raise EmbeddingError('invalid_citations')
                            seen.add(cid)
                            row = self.permitted(conn, actor['id'], space_id, cid, lock=True)
                            if not row:
                                raise EmbeddingError('permission_changed')
                            if not isinstance(quote, str) or not 12 <= len(quote) <= 600 or quote not in row['text']:
                                raise EmbeddingError('invalid_citations')
                            offset = row['start_offset'] + row['text'].index(quote)
                            citations.append({
                                'chunk_id': cid, 'document_id': row['document_id'],
                                'version_id': row['version_id'], 'space_id': space_id,
                                'title': row['title'], 'quote': quote,
                                'start_offset': offset, 'end_offset': offset + len(quote),
                            })
                        answer, outcome = result['answer'].strip(), 'answered'
        except EmbeddingError as failure:
            outcome = str(failure) if str(failure) in {
                'model_busy', 'model_unavailable', 'model_changed', 'model_not_installed',
                'invalid_embedding', 'incomplete_generation', 'invalid_generation',
                'invalid_citations', 'permission_changed', 'prompt_budget',
            } else 'model_unavailable'
            answer, citations = '', []
        handoff = outcome != 'answered'
        if handoff:
            answer, citations = 'I cannot answer from your current permitted notes. Contact a service operator.', []
        metadata = {**metadata, 'chunk_ids': [r['chunk_id'] for r in citations],
                    'query_sha256': hashlib.sha256(question.encode()).hexdigest()}
        with self.db.connect() as conn:
            self.db.run(conn, 'INSERT INTO rag_traces VALUES (?,?,?,?,?,?)', (
                trace_id, actor['id'], space_id, outcome,
                (time.perf_counter()-started)*1000, json.dumps(metadata),
            ))
        return {'answer': answer, 'citations': citations, 'handoff': handoff,
                'reason': outcome, 'trace_id': trace_id, 'mode': 'local-model-rag',
                'storage': 'pgvector' if self.db.postgres else 'sqlite-exact-scan'}


def install_routes(app, knowledge, embedder=None, generator=None):
    service = RagService(app.state.db, embedder, generator)
    app.state.rag = service
    if embedder:
        knowledge.register_space(embedder.space)
    router = APIRouter(prefix='/rag')

    def identity(authorization: str = Header(default='')):
        return service.authenticate(authorization)

    def ingest_role(actor=Depends(identity)):
        if actor['role'] not in {'operator', 'admin'}:
            raise HTTPException(403, 'Operator role required')
        return actor

    def admin(actor=Depends(identity)):
        if actor['role'] != 'admin':
            raise HTTPException(403, 'Admin role required')
        return actor

    @router.get('/me')
    def me(actor=Depends(identity)):
        return actor

    @router.post('/chat')
    def chat(request: RagRequest, actor=Depends(identity)):
        return service.answer(actor, request.message)

    @router.get('/chunks/{chunk_id}')
    def chunk(chunk_id: str, space_id: str, actor=Depends(identity)):
        with service.db.connect() as conn:
            row = service.permitted(conn, actor['id'], space_id, chunk_id)
            if not row:
                raise HTTPException(404, 'Unknown permitted source')
            return dict(row)

    @router.put('/documents/{document_id}/grants/{identity_id}', dependencies=[Depends(admin)])
    def grant(document_id: str, identity_id: str, request: Grant):
        set_grant(service.db, document_id, identity_id, request.enabled)
        return {'enabled': request.enabled}

    @router.put('/documents/{document_id}/enabled', dependencies=[Depends(admin)])
    def enabled(document_id: str, request: Grant):
        with service.db.connect() as conn:
            if not service.db.run(conn, 'SELECT id FROM knowledge_documents WHERE id=?', (document_id,)).fetchone():
                raise HTTPException(404, 'Unknown document')
            service.db.run(conn, '''INSERT INTO document_permissions VALUES (?,?)
                ON CONFLICT(document_id) DO UPDATE SET enabled=excluded.enabled''',
                (document_id, int(request.enabled)))
        return {'enabled': request.enabled}

    @router.get('/traces/{trace_id}')
    def trace(trace_id: str, actor=Depends(identity)):
        with service.db.connect() as conn:
            row = service.db.run(conn, 'SELECT * FROM rag_traces WHERE id=? AND identity_id=?',
                                 (trace_id, actor['id'])).fetchone()
            if not row:
                raise HTTPException(404, 'Unknown trace')
            return dict(row)

    # Import here avoids the application factory's request-model import cycle.
    from app import KnowledgeDocument

    @router.post('/documents', status_code=202)
    def ingest(doc: KnowledgeDocument, actor=Depends(ingest_role)):
        try:
            space = doc.space_id or (embedder.space.id if embedder else None)
            if not space:
                raise HTTPException(503, 'Configure an embedding space first')
            return knowledge.ingest(doc.source_key, doc.title, doc.body, space, actor['id'])
        except PermissionError as failure:
            raise HTTPException(403, str(failure)) from None
        except ValueError as failure:
            raise HTTPException(422, str(failure)) from None

    @router.get('/jobs/{job_id}')
    def job(job_id: str, actor=Depends(ingest_role)):
        result = knowledge.job(job_id)
        if result is None:
            raise HTTPException(404, 'Unknown index job')
        with service.db.connect() as conn:
            if not permitted_document(service.db, conn, result['document_id'], actor['id']):
                raise HTTPException(404, 'Unknown index job')
        return result

    @router.get('/config')
    def config(actor=Depends(identity)):
        return {'storage': 'pgvector' if service.db.postgres else 'SQLite exact scan',
                'embedding': None if not embedder else {'id': embedder.space.id, 'backend': embedder.space.backend,
                    'model': embedder.space.model, 'dimensions': embedder.space.dimensions},
                'generation_model': getattr(generator, 'model', None), 'scope': 'Configured models, not a live health probe'}

    @router.get('/spaces', dependencies=[Depends(ingest_role)])
    def spaces():
        return knowledge.spaces()

    @router.get('/identities', dependencies=[Depends(admin)])
    def identities():
        with service.db.connect() as conn:
            return [dict(r) for r in service.db.run(conn, 'SELECT id,name,role,enabled FROM rag_identities ORDER BY name').fetchall()]

    @router.get('/documents')
    def documents(actor=Depends(ingest_role)):
        with service.db.connect() as conn:
            rows = service.db.run(conn, '''SELECT d.id,d.source_key FROM knowledge_documents d
                JOIN document_grants g ON g.document_id=d.id AND g.identity_id=? AND g.enabled=1
                JOIN document_permissions p ON p.document_id=d.id AND p.enabled=1 ORDER BY d.created_at DESC,d.id LIMIT 50''', (actor['id'],)).fetchall()
            return [dict(row) for row in rows]

    @router.get('/jobs')
    def jobs(actor=Depends(ingest_role)):
        with service.db.connect() as conn:
            rows = service.db.run(conn, '''SELECT j.id FROM index_jobs j JOIN document_versions v ON v.id=j.version_id
                JOIN document_grants g ON g.document_id=v.document_id AND g.identity_id=? AND g.enabled=1
                JOIN document_permissions p ON p.document_id=v.document_id AND p.enabled=1 ORDER BY j.created_at DESC,j.id LIMIT 50''', (actor['id'],)).fetchall()
        return [knowledge.job(row['id']) for row in rows]

    app.include_router(router)
    from reviews import install_review_routes
    install_review_routes(app, service, identity, ingest_role)


def permitted_document(db, conn, document_id, actor_id):
    return db.run(conn, '''SELECT g.document_id FROM document_grants g JOIN document_permissions p ON p.document_id=g.document_id
        WHERE g.document_id=? AND g.identity_id=? AND g.enabled=1 AND p.enabled=1''', (document_id, actor_id)).fetchone()
