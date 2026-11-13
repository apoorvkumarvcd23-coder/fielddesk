"""Opt-in handoff review, human approval and identity-bound links to existing tickets."""
import hashlib
import json
import time
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

class Request(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class ReviewRequest(Request):
    trace_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    consent: bool = Field(strict=True)

class Revision(Request):
    expected_revision: int = Field(ge=0, strict=True)

class Resolution(Revision):
    resolution: str = Field(min_length=1, max_length=600)

class Proposal(Revision):
    title: str = Field(min_length=1, max_length=300)

class ReviewStore:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock

    def purge(self):
        with self.db.connect() as conn:
            return self.db.run(conn, """UPDATE rag_reviews SET question='',resolution='Review retention expired',
                state='resolved',revision=revision+1,deleted_at=? WHERE expires_at<=? AND question<>''""",
                (self.clock(), self.clock())).rowcount

    def actor(self, conn, supplied, operator=False):
        suffix = ' FOR UPDATE' if self.db.postgres else ''
        row = self.db.run(conn, 'SELECT id,name,role FROM rag_identities WHERE id=? AND enabled=1' + suffix,
                          (supplied['id'],)).fetchone()
        if not row:
            raise HTTPException(401, 'Identity disabled')
        actor = dict(row)
        if operator and actor['role'] not in {'operator','admin'}:
            raise HTTPException(403, 'Operator required')
        return actor

    def load(self, conn, review_id, actor, lock=False):
        suffix = ' FOR UPDATE OF r' if lock and self.db.postgres else ''
        row = self.db.run(conn, """SELECT r.*,i.name AS requester FROM rag_reviews r
            JOIN rag_identities i ON i.id=r.requester_id WHERE r.id=?""" + suffix, (review_id,)).fetchone()
        if not row or (actor['role']=='user' and row['requester_id']!=actor['id']):
            raise HTTPException(404, 'Unknown review')
        return dict(row)

    def view(self, conn, row):
        result = dict(row)
        action = self.db.run(conn, """SELECT a.id,a.title,a.status,a.created_at,a.ticket_id,l.approved_by
            FROM actions a JOIN rag_ticket_links l ON l.action_id=a.id WHERE l.review_id=?""", (row['id'],)).fetchone()
        result['action'] = dict(action) if action else None
        result['events'] = [dict(event) for event in self.db.run(conn,
            'SELECT actor_id,event,created_at FROM rag_review_events WHERE review_id=? ORDER BY created_at,id', (row['id'],)).fetchall()]
        return result

    def event(self, conn, review_id, actor_id, kind):
        self.db.run(conn, 'INSERT INTO rag_review_events VALUES (?,?,?,?,?)',
                    (str(uuid4()), review_id, actor_id, kind, self.clock()))

    def create(self, actor, trace_id, message, consent):
        if consent is not True:
            raise HTTPException(422, 'Explicit sharing consent required')
        self.purge()
        with self.db.connect() as conn:
            actor = self.actor(conn, actor)
            trace = self.db.run(conn, 'SELECT * FROM rag_traces WHERE id=? AND identity_id=?', (trace_id, actor['id'])).fetchone()
            if not trace:
                raise HTTPException(404, 'Unknown trace')
            if trace['outcome']=='answered' or json.loads(trace['metadata']).get('query_sha256')!=hashlib.sha256(message.encode()).hexdigest():
                raise HTTPException(409, 'A matching handoff trace is required; ask the question again')
            previous = self.db.run(conn, 'SELECT id FROM rag_reviews WHERE trace_id=?', (trace_id,)).fetchone()
            if previous:
                return self.view(conn, self.load(conn, previous['id'], actor))
            count = self.db.run(conn, """SELECT count(*) AS n FROM rag_reviews
                WHERE requester_id=? AND state<>'resolved' AND expires_at>?""", (actor['id'], self.clock())).fetchone()['n']
            if count >= 20:
                raise HTTPException(429, 'At most 20 open reviews per identity')
            review_id, now = str(uuid4()), self.clock()
            self.db.run(conn, """INSERT INTO rag_reviews
                (id,trace_id,requester_id,question,state,created_at,expires_at) VALUES (?,?,?,?,'open',?,?)""",
                (review_id, trace_id, actor['id'], message, now, now+7*86400))
            self.event(conn, review_id, actor['id'], 'submitted')
            return self.view(conn, self.load(conn, review_id, actor))

    def listing(self, actor):
        self.purge()
        with self.db.connect() as conn:
            actor = self.actor(conn, actor)
            where, params = ("WHERE requester_id=?", (actor['id'],)) if actor['role']=='user' else ('', ())
            rows = self.db.run(conn, 'SELECT id FROM rag_reviews '+where+' ORDER BY created_at DESC,id LIMIT 50', params).fetchall()
            return [self.view(conn, self.load(conn, row['id'], actor)) for row in rows]

    def get(self, actor, review_id):
        self.purge()
        with self.db.connect() as conn:
            actor = self.actor(conn, actor)
            return self.view(conn, self.load(conn, review_id, actor))

    @staticmethod
    def revision(row, expected):
        if row['revision']!=expected:
            raise HTTPException(409, 'Review changed; refresh before continuing')

    def assigned(self, row, actor):
        if row['deleted_at'] is not None or row['expires_at']<=self.clock() or row['state']!='in_review':
            raise HTTPException(409, 'Review is not active')
        if row['assigned_to']!=actor['id']:
            raise HTTPException(403, 'Only the assigned operator can continue')

    def claim(self, actor, review_id, expected):
        self.purge()
        with self.db.connect() as conn:
            actor = self.actor(conn, actor, True)
            row = self.load(conn, review_id, actor, True)
            if row['state']=='in_review' and row['assigned_to']==actor['id']:
                return self.view(conn, row)
            self.revision(row, expected)
            if row['state']!='open' or row['expires_at']<=self.clock() or row['deleted_at'] is not None:
                raise HTTPException(409, 'Review cannot be claimed')
            self.db.run(conn, "UPDATE rag_reviews SET state='in_review',assigned_to=?,revision=revision+1 WHERE id=?", (actor['id'], review_id))
            self.event(conn, review_id, actor['id'], 'claimed')
            return self.view(conn, self.load(conn, review_id, actor))

    def resolve(self, actor, review_id, expected, resolution):
        self.purge()
        with self.db.connect() as conn:
            actor = self.actor(conn, actor, True)
            row = self.load(conn, review_id, actor, True)
            if row['state']=='resolved' and row['assigned_to']==actor['id'] and row['resolution']==resolution:
                return self.view(conn, row)
            self.assigned(row, actor)
            self.revision(row, expected)
            self.db.run(conn, "UPDATE rag_reviews SET state='resolved',resolution=?,revision=revision+1 WHERE id=?", (resolution, review_id))
            self.event(conn, review_id, actor['id'], 'resolved')
            return self.view(conn, self.load(conn, review_id, actor))

    def withdraw(self, actor, review_id):
        with self.db.connect() as conn:
            actor = self.actor(conn, actor)
            row = self.load(conn, review_id, actor, True)
            if row['requester_id']!=actor['id']:
                raise HTTPException(403, 'Only the requester can withdraw shared text')
            if row['deleted_at'] is None:
                self.db.run(conn, """UPDATE rag_reviews SET question='',resolution='Requester withdrew shared text',
                    state='resolved',deleted_at=?,revision=revision+1 WHERE id=?""", (self.clock(), review_id))
                self.event(conn, review_id, actor['id'], 'withdrawn')
            return self.view(conn, self.load(conn, review_id, actor))

    def propose(self, actor, review_id, expected, title):
        self.purge()
        with self.db.connect() as conn:
            actor = self.actor(conn, actor, True)
            row = self.load(conn, review_id, actor, True)
            self.assigned(row, actor)
            previous = self.view(conn, row)['action']
            if previous:
                if previous['title']!=title:
                    raise HTTPException(409, 'This review already has a different ticket proposal')
                if self.clock()-previous['created_at']<=900:
                    return self.view(conn, row)
                self.revision(row, expected)
                self.db.run(conn, 'UPDATE actions SET created_at=? WHERE id=?', (self.clock(), previous['id']))
                self.event(conn, review_id, actor['id'], 'proposal_refreshed')
            else:
                self.revision(row, expected)
                action_id = str(uuid4())
                self.db.run(conn, 'INSERT INTO actions VALUES (?,?,?,?,?,NULL)',
                            (action_id, 'rag-review:'+review_id, title, 'pending', self.clock()))
                self.db.run(conn, 'INSERT INTO rag_ticket_links VALUES (?,?,?,NULL)', (action_id, review_id, row['requester_id']))
                self.event(conn, review_id, actor['id'], 'ticket_proposed')
            self.db.run(conn, 'UPDATE rag_reviews SET revision=revision+1 WHERE id=?', (review_id,))
            return self.view(conn, self.load(conn, review_id, actor))

    def approve(self, actor, review_id, expected):
        self.purge()
        with self.db.connect() as conn:
            actor = self.actor(conn, actor, True)
            row = self.load(conn, review_id, actor, True)
            if row['requester_id']==actor['id']:
                raise HTTPException(403, 'Self-approval is forbidden')
            action = self.view(conn, row)['action']
            if not action:
                raise HTTPException(409, 'Propose a ticket first')
            if row['assigned_to']!=actor['id']:
                raise HTTPException(403, 'Only the assigned operator can approve')
            if action['status']=='approved':
                return self.view(conn, row)
            self.assigned(row, actor)
            self.revision(row, expected)
            if self.clock()-action['created_at']>900:
                raise HTTPException(409, 'Proposal expired; explicitly propose the same title again to refresh')
            ticket_id = str(uuid4())
            self.db.run(conn, 'INSERT INTO tickets VALUES (?,?,?)', (ticket_id, action['title'], 'open'))
            self.db.run(conn, "UPDATE actions SET status='approved',ticket_id=? WHERE id=?", (ticket_id, action['id']))
            self.db.run(conn, 'UPDATE rag_ticket_links SET approved_by=? WHERE action_id=?', (actor['id'], action['id']))
            self.db.run(conn, "UPDATE rag_reviews SET state='resolved',resolution='Approved service ticket',revision=revision+1 WHERE id=?", (review_id,))
            self.event(conn, review_id, actor['id'], 'ticket_approved')
            return self.view(conn, self.load(conn, review_id, actor))

    def ticket(self, actor, ticket_id):
        with self.db.connect() as conn:
            actor = self.actor(conn, actor)
            row = self.db.run(conn, """SELECT t.*,l.requester_id FROM tickets t JOIN actions a ON a.ticket_id=t.id
                JOIN rag_ticket_links l ON l.action_id=a.id WHERE t.id=?""", (ticket_id,)).fetchone()
            if not row or (actor['role']=='user' and row['requester_id']!=actor['id']):
                raise HTTPException(404, 'Unknown ticket')
            return {key: row[key] for key in ('id','title','status')}

def install_review_routes(app, service, identity, ingest_role):
    store = ReviewStore(service.db)
    app.state.reviews = store
    router = APIRouter(prefix='/rag')
    @router.post('/reviews')
    def submit(request: ReviewRequest, actor=Depends(identity)):
        return store.create(actor, request.trace_id, request.message, request.consent)
    @router.get('/reviews')
    def listing(actor=Depends(identity)):
        return store.listing(actor)
    @router.get('/reviews/{review_id}')
    def get(review_id: str, actor=Depends(identity)):
        return store.get(actor, review_id)
    @router.delete('/reviews/{review_id}')
    def withdraw(review_id: str, actor=Depends(identity)):
        return store.withdraw(actor, review_id)
    @router.post('/reviews/{review_id}/claim')
    def claim(review_id: str, request: Revision, actor=Depends(ingest_role)):
        return store.claim(actor, review_id, request.expected_revision)
    @router.post('/reviews/{review_id}/resolve')
    def resolve(review_id: str, request: Resolution, actor=Depends(ingest_role)):
        return store.resolve(actor, review_id, request.expected_revision, request.resolution)
    @router.post('/reviews/{review_id}/proposal')
    def propose(review_id: str, request: Proposal, actor=Depends(ingest_role)):
        return store.propose(actor, review_id, request.expected_revision, request.title)
    @router.post('/reviews/{review_id}/approve')
    def approve(review_id: str, request: Revision, actor=Depends(ingest_role)):
        return store.approve(actor, review_id, request.expected_revision)
    @router.get('/tickets/{ticket_id}')
    def ticket(ticket_id: str, actor=Depends(identity)):
        return store.ticket(actor, ticket_id)
    app.include_router(router)
