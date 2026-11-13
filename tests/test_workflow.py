"""Workflow authorization/atomicity tests; inference adapters in rig are deterministic, not model proof."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import pytest
from fastapi import HTTPException
from test_rag import rig  # Reuse the explicit deterministic-adapter fixture.
from knowledge import run_once
from embeddings import HashEmbedder

def auth(rig, name):
    return rig[3][name]['headers']

def handoff(rig, name='alice', question='I need an operator for projector repair'):
    client = rig[0]
    result = client.post('/rag/chat', headers=auth(rig,name), json={'message':question}).json()
    assert result['handoff']
    return client.post('/rag/reviews', headers=auth(rig,name), json={
        'trace_id':result['trace_id'],'message':question,'consent':True})

def post(rig, review, action, name='operator', **payload):
    return rig[0].post('/rag/reviews/'+review['id']+'/'+action, headers=auth(rig,name),
                      json={'expected_revision':review['revision'],**payload})

def count(rig, table):
    assert table in {'tickets','actions','rag_reviews','rag_review_events'}
    db=rig[1].state.db
    with db.connect() as conn:
        return db.run(conn,'SELECT count(*) AS n FROM '+table).fetchone()['n']

def proposed(rig):
    row=handoff(rig).json()
    row=post(rig,row,'claim').json()
    return post(rig,row,'proposal',title='Synthetic projector repair inspection').json()

def test_review_requires_consent_matching_handoff_and_trace_ownership(rig):
    client=rig[0]
    question='I need an operator for projector repair'
    result=client.post('/rag/chat',headers=auth(rig,'alice'),json={'message':question}).json()
    assert client.get('/rag/reviews',headers=auth(rig,'operator')).json()==[]
    payload={'trace_id':result['trace_id'],'message':question,'consent':True}
    for value in [False,'true',1]:
        assert client.post('/rag/reviews',headers=auth(rig,'alice'),json={**payload,'consent':value}).status_code==422
    assert client.post('/rag/reviews',headers=auth(rig,'bob'),json=payload).status_code==404
    assert client.post('/rag/reviews',headers=auth(rig,'alice'),json={**payload,'message':'another question'}).status_code==409
    first=client.post('/rag/reviews',headers=auth(rig,'alice'),json=payload).json()
    replay=client.post('/rag/reviews',headers=auth(rig,'alice'),json=payload).json()
    assert first['id']==replay['id'] and count(rig,'rag_reviews')==1
    assert client.get('/rag/reviews/'+first['id'],headers=auth(rig,'bob')).status_code==404
    assert client.get('/rag/reviews',headers=auth(rig,'bob')).json()==[]
    trace=client.get('/rag/traces/'+result['trace_id'],headers=auth(rig,'alice')).json()
    assert json.loads(trace['metadata'])['query_sha256']==hashlib.sha256(question.encode()).hexdigest()
    assert question not in json.dumps(trace)

def test_answered_trace_cannot_be_fabricated_into_handoff_review(rig):
    question='projector repair service ticket'
    result=rig[0].post('/rag/chat',headers=auth(rig,'alice'),json={'message':question}).json()
    assert not result['handoff']
    assert rig[0].post('/rag/reviews',headers=auth(rig,'alice'),json={
        'trace_id':result['trace_id'],'message':question,'consent':True}).status_code==409

def test_legacy_keys_cannot_overwrite_identity_sources_or_reserve_review_actions(rig):
    client=rig[0]
    assert client.post('/knowledge/documents',headers={'Authorization':'Bearer fixture-operator'},json={
        'source_key':'projector','title':'Untrusted replacement','body':'Different source content'}).status_code==403
    row=handoff(rig).json()
    assert client.post('/actions',headers={'Authorization':'Bearer fixture-user'},json={
        'request_key':'rag-review:'+row['id'],'title':'Reserve future identity action'}).status_code==409
    assert count(rig,'actions')==0

def test_claim_revision_assignment_and_resolve_are_persisted(rig):
    row=handoff(rig).json()
    assert post(rig,row,'claim',name='alice').status_code==403
    old=dict(row);row=post(rig,row,'claim').json()
    assert row['state']=='in_review' and row['revision']==1
    assert post(rig,old,'claim',name='admin').status_code==409
    assert post(rig,row,'resolve',name='admin',resolution='Other operator').status_code==403
    assert post(rig,old,'resolve',resolution='Stale edit').status_code==409
    saved=post(rig,row,'resolve',resolution='Handled through campus desk').json()
    assert saved['state']=='resolved' and len(saved['events'])==3
    assert post(rig,row,'resolve',resolution='Handled through campus desk').json()['revision']==saved['revision']
    assert count(rig,'tickets')==0
    assert post(rig,saved,'proposal',title='Not active').status_code==409

def test_explicit_proposal_approval_replay_and_legacy_isolation(rig):
    row=proposed(rig)
    assert row['action']['status']=='pending' and count(rig,'tickets')==0
    assert post(rig,row,'proposal',title=row['action']['title']).json()['action']['id']==row['action']['id']
    assert post(rig,row,'proposal',title='Changed title').status_code==409
    assert post(rig,row,'approve',name='alice').status_code==403
    assert post(rig,row,'approve',name='admin').status_code==403
    assert rig[0].post('/actions/'+row['action']['id']+'/approve',headers={'Authorization':'Bearer fixture-operator'},json={}).status_code==404
    assert rig[0].get('/actions',headers={'Authorization':'Bearer fixture-operator'}).json()==[]
    assert rig[0].post('/actions',headers={'Authorization':'Bearer fixture-user'},json={
        'request_key':'rag-review:'+row['id'],'title':row['action']['title']}).status_code==409
    saved=post(rig,row,'approve').json()
    assert saved['state']=='resolved' and saved['action']['approved_by']==rig[3]['operator']['id']
    ticket=saved['action']['ticket_id']
    assert post(rig,row,'approve').json()['action']['ticket_id']==ticket
    assert count(rig,'tickets')==count(rig,'actions')==1
    assert rig[0].get('/rag/tickets/'+ticket,headers=auth(rig,'alice')).status_code==200
    assert rig[0].get('/rag/tickets/'+ticket,headers=auth(rig,'bob')).status_code==404
    assert rig[0].get('/tickets/'+ticket,headers={'Authorization':'Bearer fixture-user'}).status_code==404

def test_concurrent_approval_creates_one_ticket_and_one_approval_event(rig):
    row=proposed(rig)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _: post(rig,row,'approve'),range(3)))
    assert all(result.status_code==200 for result in results)
    assert len({result.json()['action']['ticket_id'] for result in results})==1
    assert count(rig,'tickets')==1
    assert [e['event'] for e in results[0].json()['events']].count('ticket_approved')==1

def test_audit_failure_rolls_back_ticket_and_review_then_retry_succeeds(rig,monkeypatch):
    row=proposed(rig);db=rig[1].state.db;original=db.run
    def failing(conn,sql,params=()):
        if 'INSERT INTO rag_review_events' in sql:
            raise RuntimeError('synthetic audit publication failure')
        return original(conn,sql,params)
    monkeypatch.setattr(db,'run',failing)
    with pytest.raises(RuntimeError,match='audit publication'):
        post(rig,row,'approve')
    monkeypatch.setattr(db,'run',original)
    assert count(rig,'tickets')==0
    current=rig[0].get('/rag/reviews/'+row['id'],headers=auth(rig,'operator')).json()
    assert current['state']=='in_review' and current['action']['status']=='pending'
    assert post(rig,current,'approve').status_code==200

def test_disabled_and_changed_roles_cannot_use_cached_authority_or_self_approve(rig):
    row=handoff(rig).json();db=rig[1].state.db
    with db.connect() as conn:
        db.run(conn,"UPDATE rag_identities SET role='operator' WHERE id=?",(rig[3]['alice']['id'],))
    row=post(rig,row,'claim',name='alice').json()
    row=post(rig,row,'proposal',name='alice',title='Own proposal').json()
    assert post(rig,row,'approve',name='alice').status_code==403
    with db.connect() as conn:
        db.run(conn,"UPDATE rag_identities SET enabled=0 WHERE id=?",(rig[3]['alice']['id'],))
    assert rig[0].get('/rag/reviews',headers=auth(rig,'alice')).status_code==401
    with pytest.raises(HTTPException) as error:
        rig[1].state.reviews.claim({'id':rig[3]['alice']['id']},row['id'],row['revision'])
    assert error.value.status_code==401
    assert count(rig,'tickets')==0

def test_withdrawal_and_expiry_redact_text_without_duplication(rig):
    row=handoff(rig).json();client=rig[0]
    assert client.delete('/rag/reviews/'+row['id'],headers=auth(rig,'bob')).status_code==404
    assert client.delete('/rag/reviews/'+row['id'],headers=auth(rig,'operator')).status_code==403
    for _ in range(2):
        saved=client.delete('/rag/reviews/'+row['id'],headers=auth(rig,'alice')).json()
        assert saved['question']=='' and saved['state']=='resolved'
    assert [e['event'] for e in saved['events']].count('withdrawn')==1
    second=handoff(rig,question='I need an operator for another repair').json()
    rig[1].state.reviews.clock=lambda:second['expires_at']+1
    rows=client.get('/rag/reviews',headers=auth(rig,'operator')).json()
    assert all(r['question']=='' for r in rows)
    assert post(rig,second,'claim').status_code==409

def test_expired_proposal_needs_explicit_refresh_before_approval(rig):
    row=proposed(rig)
    rig[1].state.reviews.clock=lambda:row['action']['created_at']+901
    assert post(rig,row,'approve').status_code==409
    refreshed=post(rig,row,'proposal',title=row['action']['title']).json()
    assert refreshed['action']['id']==row['action']['id']
    assert refreshed['revision']==row['revision']+1
    assert post(rig,refreshed,'approve').status_code==200

def test_open_review_quota_is_actor_scoped(rig):
    for i in range(20):
        assert handoff(rig,question='I need an operator for case '+str(i)).status_code==200
    assert handoff(rig,question='I need an operator for case 21').status_code==429
    assert handoff(rig,name='bob').status_code==200

def test_identity_ingest_is_atomic_granted_and_cannot_replace_another_source(rig):
    client,app,_,actors,public,private=rig
    payload={'source_key':'new-operator-note','title':'Operator note','body':'Synthetic projector inspections need a service ticket.','space_id':HashEmbedder.space.id}
    first=client.post('/rag/documents',headers=auth(rig,'operator'),json=payload)
    assert first.status_code==202
    job=first.json()
    assert client.post('/rag/documents',headers=auth(rig,'operator'),json=payload).json()['id']==job['id']
    assert client.post('/rag/documents',headers=auth(rig,'admin'),json={**payload,'body':'unauthorized replacement'}).status_code==403
    assert client.get('/rag/jobs/'+job['id'],headers=auth(rig,'operator')).status_code==200
    assert client.get('/rag/jobs/'+job['id'],headers=auth(rig,'admin')).status_code==404
    assert client.get('/rag/jobs/'+private['id'],headers=auth(rig,'operator')).status_code==404
    assert client.get('/rag/documents',headers=auth(rig,'admin')).json()==[]
    assert client.get('/rag/documents',headers=auth(rig,'operator')).json()==[{'id':job['document_id'],'source_key':payload['source_key']}]
    assert client.get('/rag/jobs',headers=auth(rig,'operator')).json()[0]['id']==job['id']
    assert client.get('/rag/spaces',headers=auth(rig,'alice')).status_code==403
    assert client.get('/rag/identities',headers=auth(rig,'operator')).status_code==403
    identities=client.get('/rag/identities',headers=auth(rig,'admin')).json()
    assert all(set(identity)=={'id','name','role','enabled'} for identity in identities)
    run_once(app.state.knowledge,HashEmbedder())
    assert client.get('/rag/jobs/'+job['id'],headers=auth(rig,'operator')).json()['status']=='ready'
