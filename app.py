"""Offline retrieval/approval plus operator-only versioned indexing foundation."""
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from embeddings import HashEmbedder, embedding
from knowledge import KnowledgeStore
from storage import Database


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Document(Strict):
    title: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=20000)


class Chat(Strict):
    message: str = Field(min_length=1, max_length=2000)


class KnowledgeDocument(Document):
    # Source offsets refer to exact accepted text, including edge whitespace.
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    source_key: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$')
    space_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class Action(Strict):
    request_key: str = Field(min_length=8, max_length=100)
    title: str = Field(min_length=1, max_length=300)


def create_app(database_url=None, user_key=None, operator_key=None, rag_embedder=None, rag_generator=None):
    db = Database(database_url or os.getenv('DATABASE_URL', 'fielddesk.db'))
    user_key = user_key or os.getenv('FIELDDESK_USER_KEY')
    operator_key = operator_key or os.getenv('FIELDDESK_OPERATOR_KEY')
    if not user_key or not operator_key or user_key == operator_key:
        raise RuntimeError('Set distinct FIELDDESK_USER_KEY and FIELDDESK_OPERATOR_KEY.')
    db.initialize()
    app = FastAPI(title='FieldDesk', version='0.1.0')
    app.state.db = db
    knowledge = KnowledgeStore(db)
    knowledge.register_space(HashEmbedder.space)
    app.state.knowledge = knowledge

    def identity(authorization: str = Header(default='')):
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Bearer credentials required')
        token = authorization.removeprefix('Bearer ')
        if secrets.compare_digest(token, operator_key):
            return 'operator'
        if secrets.compare_digest(token, user_key):
            return 'user'
        raise HTTPException(401, 'Invalid credentials')

    def operator(role=Depends(identity)):
        if role != 'operator':
            raise HTTPException(403, 'Operator approval required')

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(Path(__file__).parent / 'index.html')

    @app.get('/baseline', include_in_schema=False)
    def baseline():
        return FileResponse(Path(__file__).parent / 'baseline.html')

    @app.get('/workspace.js', include_in_schema=False)
    def workspace_script():
        return FileResponse(Path(__file__).parent / 'workspace.js', media_type='text/javascript')

    @app.get('/health')
    def health():
        return {'status': 'ok', 'storage': 'pgvector' if db.postgres else 'sqlite-vector-baseline'}

    @app.get('/knowledge/spaces', dependencies=[Depends(operator)])
    def spaces():
        return knowledge.spaces()

    @app.post('/knowledge/documents', dependencies=[Depends(operator)], status_code=202)
    def ingest_knowledge(doc: KnowledgeDocument):
        try:
            return knowledge.ingest(doc.source_key, doc.title, doc.body, doc.space_id, legacy_api=True)
        except PermissionError as failure:
            raise HTTPException(403, str(failure)) from None
        except ValueError as failure:
            raise HTTPException(422, str(failure)) from None

    @app.get('/knowledge/jobs/{job_id}', dependencies=[Depends(operator)])
    def index_status(job_id: str):
        result = knowledge.job(job_id)
        if result is None:
            raise HTTPException(404, 'Unknown index job')
        return result

    @app.get('/knowledge/documents/{document_id}', dependencies=[Depends(operator)])
    def knowledge_document(document_id: str):
        result = knowledge.document(document_id)
        if result is None:
            raise HTTPException(404, 'Unknown knowledge document')
        return result

    @app.get('/knowledge/versions/{version_id}/chunks', dependencies=[Depends(operator)])
    def knowledge_chunks(version_id: str, space_id: str = HashEmbedder.space.id):
        result = knowledge.ready_chunks(version_id, space_id)
        if result is None:
            raise HTTPException(409, 'Version index is not ready in this embedding space')
        return result

    @app.post('/documents', dependencies=[Depends(operator)], status_code=201)
    def ingest(doc: Document):
        doc_id = hashlib.sha256((doc.title + '\0' + doc.body).encode()).hexdigest()
        with db.connect() as c:
            db.run(c, 'INSERT INTO documents VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING',
                   (doc_id, doc.title, doc.body, json.dumps(embedding(doc.body))))
        return {'id': doc_id}

    @app.post('/chat', dependencies=[Depends(identity)])
    def chat(request: Chat):
        started = time.perf_counter()
        trace_id = str(uuid4())
        explicit_handoff = bool(re.search(r'\b(human|operator|person|ignore instructions|reveal secret)\b', request.message, re.I))
        vector = embedding(request.message)
        with db.connect() as c:
            if db.postgres:
                rows = db.run(c, 'SELECT id,title,body,1-(embedding <=> ?::vector) AS score FROM documents ORDER BY embedding <=> ?::vector LIMIT 3',
                              (json.dumps(vector), json.dumps(vector))).fetchall()
            else:
                rows = [dict(r) for r in c.execute('SELECT * FROM documents').fetchall()]
                for row in rows:
                    row['score'] = sum(a * b for a, b in zip(vector, json.loads(row['embedding'])))
                rows = sorted(rows, key=lambda r: r['score'], reverse=True)[:3]
            relevant = [r for r in rows if r['score'] is not None and r['score'] >= 0.15]
            handoff = explicit_handoff or not relevant
            citations = [] if handoff else [{'id': r['id'], 'title': r['title'], 'quote': r['body'][:700]} for r in relevant]
            answer = 'Please contact a service operator; I cannot resolve this from the approved notes.' if handoff else '\n\n'.join(f"[{i+1}] {r['title']}: {r['quote']}" for i, r in enumerate(citations))
            db.run(c, 'INSERT INTO traces VALUES (?,?,?,?)', (trace_id, 'retrieval', (time.perf_counter()-started)*1000, 'handoff' if handoff else 'answered'))
        return {'answer': answer, 'citations': citations, 'handoff': handoff, 'trace_id': trace_id, 'mode': 'offline-extractive'}

    @app.post('/actions', dependencies=[Depends(identity)], status_code=201)
    def propose(request: Action):
        if request.request_key.startswith('rag-review:'):
            raise HTTPException(409, 'Request key namespace is reserved')
        with db.connect() as c:
            db.run(c, 'INSERT INTO actions VALUES (?,?,?,?,?,?) ON CONFLICT(request_key) DO NOTHING',
                   (str(uuid4()), request.request_key, request.title, 'pending', time.time(), None))
            row = dict(db.run(c, 'SELECT * FROM actions WHERE request_key=?', (request.request_key,)).fetchone())
            if db.run(c, 'SELECT action_id FROM rag_ticket_links WHERE action_id=?', (row['id'],)).fetchone():
                raise HTTPException(409, 'Request key is unavailable')
            if row['title'] != request.title:
                raise HTTPException(409, 'Idempotency key already used for another request')
            return row

    @app.get('/actions', dependencies=[Depends(operator)])
    def actions():
        with db.connect() as c:
            return [dict(r) for r in db.run(c, 'SELECT * FROM actions WHERE id NOT IN (SELECT action_id FROM rag_ticket_links) ORDER BY created_at DESC LIMIT 50').fetchall()]

    @app.post('/actions/{action_id}/approve', dependencies=[Depends(operator)])
    def approve(action_id: str):
        with db.connect() as c:
            suffix = ' FOR UPDATE' if db.postgres else ''
            row = db.run(c, 'SELECT * FROM actions WHERE id=?' + suffix, (action_id,)).fetchone()
            if db.run(c, 'SELECT action_id FROM rag_ticket_links WHERE action_id=?', (action_id,)).fetchone():
                raise HTTPException(404, 'Unknown action')
            if not row:
                raise HTTPException(404, 'Unknown action')
            if row['status'] == 'approved':
                return {'ticket_id': row['ticket_id'], 'status': 'approved'}
            if time.time() - row['created_at'] > 900:
                raise HTTPException(409, 'Approval expired; create a new request')
            ticket_id = str(uuid4())
            db.run(c, 'INSERT INTO tickets VALUES (?,?,?)', (ticket_id, row['title'], 'open'))
            db.run(c, 'UPDATE actions SET status=?,ticket_id=? WHERE id=?', ('approved', ticket_id, action_id))
            return {'ticket_id': ticket_id, 'status': 'approved'}

    @app.get('/tickets/{ticket_id}', dependencies=[Depends(identity)])
    def ticket(ticket_id: str):
        with db.connect() as c:
            row = db.run(c, 'SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone()
            if db.run(c, 'SELECT l.action_id FROM rag_ticket_links l JOIN actions a ON a.id=l.action_id WHERE a.ticket_id=?', (ticket_id,)).fetchone():
                raise HTTPException(404, 'Unknown ticket')
            if not row:
                raise HTTPException(404, 'Unknown ticket')
            return dict(row)

    @app.get('/traces', dependencies=[Depends(operator)])
    def traces():
        with db.connect() as c:
            return [dict(r) for r in db.run(c, 'SELECT * FROM traces LIMIT 100').fetchall()]

    from rag import install_routes
    install_routes(app, knowledge, rag_embedder, rag_generator)
    return app
