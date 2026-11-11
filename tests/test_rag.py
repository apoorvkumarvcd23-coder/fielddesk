"""Permission matrix and structured-output failures; adapters here are deterministic."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from app import create_app
from embeddings import EmbeddingError, HashEmbedder, OllamaEmbedder
from generation import OllamaGenerator
from knowledge import run_once
from rag import add_identity, set_grant
from storage import Database


class FakeGenerator:
    def __init__(self):
        self.seen = []
        self.callback = None
        self.output = None

    def generate(self, question, evidence):
        self.seen.append(copy.deepcopy(evidence))
        if self.callback:
            self.callback()
        result = self.output or {'handoff': False, 'answer': 'A projector repair needs a service ticket.',
                                'citations': [{'chunk_id': evidence[0]['id'], 'quote': evidence[0]['text']}]}
        return result, {'model': 'test-only-not-real-inference', 'eval_count': 12}


@pytest.fixture
def rig(tmp_path):
    url = os.getenv('TEST_DATABASE_URL', str(tmp_path / 'rag.db'))
    db = Database(url)
    if db.postgres:
        from psycopg.conninfo import conninfo_to_dict
        assert conninfo_to_dict(url)['dbname'].endswith('_test')
    db.initialize()
    if db.postgres:
        with db.connect() as conn:
            conn.execute('TRUNCATE rag_traces,document_grants,document_permissions,rag_identities,index_jobs,chunk_embeddings,document_chunks,index_targets,document_versions,knowledge_documents,embedding_spaces')
    generator = FakeGenerator()
    app = create_app(url, 'fixture-user', 'fixture-operator', HashEmbedder(), generator)
    store = app.state.knowledge
    actors = {}
    for name, role in [('alice','user'), ('bob','user'), ('operator','operator'), ('admin','admin')]:
        token = 'synthetic-test-token-for-' + name
        actors[name] = {'id': add_identity(db, name, role, token), 'headers': {'Authorization': 'Bearer ' + token}}
    public = store.ingest('projector', 'Synthetic repair manual', 'Projector repair requires a service ticket before technician assignment.')
    private = store.ingest('restricted', 'Restricted marker', 'Projector repair restricted secret marker VIOLET-927 must not reach Alice.')
    while run_once(store, HashEmbedder()):
        pass
    set_grant(db, public['document_id'], actors['alice']['id'], True)
    set_grant(db, private['document_id'], actors['bob']['id'], True)
    with TestClient(app) as client:
        yield client, app, generator, actors, public, private


def ask(rig, name='alice', **payload):
    client, _, _, actors, *_ = rig
    return client.post('/rag/chat', headers=actors[name]['headers'],
                       json={'message': 'projector repair service ticket', **payload})


def test_permitted_answer_has_exact_offsets_and_filters_before_prompt(rig):
    client, app, generator, actors, public, private = rig
    result = ask(rig).json()
    assert not result['handoff'] and result['reason'] == 'answered'
    cite = result['citations'][0]
    assert cite['document_id'] == public['document_id']
    assert all(row['document_id'] != private['document_id'] for row in generator.seen[0])
    assert 'VIOLET' not in json.dumps(generator.seen)
    source = client.get('/rag/chunks/' + cite['chunk_id'], params={'space_id': cite['space_id']},
                        headers=actors['alice']['headers']).json()
    assert source['text'][cite['start_offset']:cite['end_offset']] == cite['quote']
    assert client.get('/rag/traces/' + result['trace_id'], headers=actors['alice']['headers']).status_code == 200


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer invalid'},
                                   {'Authorization': 'Bearer fixture-operator'}])
def test_denial_unauthenticated_and_legacy_keys_cannot_access_rag(rig, headers):
    assert rig[0].post('/rag/chat', headers=headers, json={'message': 'repair'}).status_code == 401


def test_denial_user_cannot_ingest_read_jobs_or_change_grants(rig):
    client, _, _, actors, public, _ = rig
    headers = actors['alice']['headers']
    assert client.post('/rag/documents', headers=headers, json={
        'title':'x', 'body':'x', 'source_key':'x'}).status_code == 403
    assert client.get('/rag/jobs/' + public['id'], headers=headers).status_code == 403
    assert client.put(f"/rag/documents/{public['document_id']}/grants/{actors['bob']['id']}",
                      headers=headers, json={'enabled': True}).status_code == 403
    assert client.put(f"/rag/documents/{public['document_id']}/enabled", headers=headers,
                      json={'enabled': False}).status_code == 403


def test_denial_cross_owner_source_wrong_space_and_trace(rig):
    client, app, _, actors, public, private = rig
    chunk = app.state.knowledge.ready_chunks(private['version_id'], private['space_id'])[0]
    assert client.get('/rag/chunks/' + chunk['id'], headers=actors['alice']['headers'],
                      params={'space_id':private['space_id']}).status_code == 404
    result = ask(rig).json()
    assert client.get('/rag/chunks/' + result['citations'][0]['chunk_id'], headers=actors['alice']['headers'],
                      params={'space_id':'f'*64}).status_code == 404
    assert client.get('/rag/traces/' + result['trace_id'], headers=actors['bob']['headers']).status_code == 404


@pytest.mark.parametrize('name', ['operator', 'admin'])
def test_denial_roles_do_not_implicitly_grant_document_read(rig, name):
    result = ask(rig, name).json()
    assert result['handoff'] and not result['citations'] and not rig[2].seen


def test_denial_client_cannot_impersonate_role_or_identity(rig):
    assert ask(rig, role='admin').status_code == 422
    assert ask(rig, identity_id=rig[3]['bob']['id']).status_code == 422
    assert rig[0].get('/knowledge/spaces', headers=rig[3]['alice']['headers']).status_code == 401


@pytest.mark.parametrize('change', ['grant', 'document', 'identity', 'version'])
def test_denial_revocation_or_replacement_during_generation_never_releases_answer(rig, change):
    _, app, generator, actors, public, _ = rig
    db = app.state.db
    def revoke():
        if change == 'grant':
            set_grant(db, public['document_id'], actors['alice']['id'], False)
        elif change == 'version':
            app.state.knowledge.ingest('projector', 'Replacement', 'Replacement projector repair service ticket policy.')
            run_once(app.state.knowledge, HashEmbedder())
        else:
            with db.connect() as conn:
                if change == 'document':
                    db.run(conn, 'UPDATE document_permissions SET enabled=0 WHERE document_id=?', (public['document_id'],))
                else:
                    db.run(conn, 'UPDATE rag_identities SET enabled=0 WHERE id=?', (actors['alice']['id'],))
    generator.callback = revoke
    result = ask(rig).json()
    assert result['handoff'] and result['reason'] == 'permission_changed' and not result['citations']
    assert 'A projector repair needs' not in result['answer']
    if change == 'identity':
        assert ask(rig).status_code == 401


@pytest.mark.parametrize('output', [
    {'handoff':False,'answer':'Invented','citations':[{'chunk_id':'unknown','quote':'invented quote'}]},
    {'handoff':False,'answer':'Invented','citations':[]},
    {'handoff':False,'answer':'Invented','citations':[{'chunk_id':[], 'quote':'invented quote'}]},
    {'handoff':'false','answer':'Invented','citations':[]},
    {'handoff':False,'answer':'Invented','citations':[], 'tool':'approve'},
])
def test_invalid_structured_output_is_never_an_answer(rig, output):
    rig[2].output = output
    result = ask(rig).json()
    assert result['handoff'] and not result['citations'] and 'Invented' not in result['answer']


def test_fabricated_quote_and_busy_outage_handoff_without_query_logging(rig):
    _, app, generator, actors, public, _ = rig
    cid = app.state.knowledge.ready_chunks(public['version_id'], public['space_id'])[0]['id']
    generator.output = {'handoff': False, 'answer':'Unsupported text',
                        'citations':[{'chunk_id':cid,'quote':'This quote does not exist.'}]}
    assert ask(rig).json()['reason'] == 'invalid_citations'
    def unavailable():
        raise EmbeddingError('model_unavailable')
    generator.callback = unavailable
    assert ask(rig).json()['reason'] == 'model_unavailable'
    with app.state.db.connect() as conn:
        rows = app.state.db.run(conn, 'SELECT * FROM rag_traces WHERE identity_id=?', (actors['alice']['id'],)).fetchall()
    assert all('projector repair service ticket' not in json.dumps(dict(row)) for row in rows)


def test_query_prefix_and_generation_transport_budgets(monkeypatch):
    calls = []
    def request(self, path, payload=None):
        calls.append((path,payload))
        if path == '/api/tags':
            return {'models':[{'name':self.model,'digest':'fixed-digest'}]}
        if path == '/api/embed':
            return {'embeddings':[[1.0,0.0] for _ in payload['input']]}
        return {'done': True, 'response':json.dumps({'handoff':True,'answer':'','citations':[]}), 'eval_count':4}
    monkeypatch.setattr(OllamaEmbedder, '_request', request)
    adapter = OllamaEmbedder()
    assert adapter.embed_query('repair') == [1.0,0.0]
    assert any(p and p.get('input') == ['search_query: repair'] for _,p in calls)
    generator = OllamaGenerator()
    generator.generate('repair', [{'id':'chunk','text':'Synthetic repair note.'}])
    payload = calls[-2][1]
    assert payload['stream'] is False and payload['think'] is False
    assert payload['options']['num_ctx'] == 4096 and payload['options']['num_predict'] == 400
    with pytest.raises(ValueError):
        OllamaGenerator(base_url='https://external.invalid')
    with pytest.raises(EmbeddingError, match='prompt_budget'):
        generator.generate('長'*2000, [{'id':'chunk','text':'Synthetic repair note.'}])
    generator.lock.acquire()
    try:
        with pytest.raises(EmbeddingError, match='model_busy'):
            generator.generate('repair', [])
    finally:
        generator.lock.release()


def test_generated_identity_setup_replays_without_token_rotation_or_plaintext_storage(tmp_path):
    environment = {**os.environ, 'DATABASE_URL': str(tmp_path / 'identities.db')}
    script = Path(__file__).resolve().parents[1] / 'rag_manage.py'
    command = [sys.executable, str(script), 'identities']
    first = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    path = tmp_path / 'credentials.json'
    initial = path.read_bytes()
    credentials = json.loads(initial)
    second = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert second.returncode == 0 and path.read_bytes() == initial
    with Database(environment['DATABASE_URL']).connect() as conn:
        stored = [dict(row) for row in conn.execute('SELECT * FROM rag_identities').fetchall()]
    for account in credentials.values():
        assert len(account['token']) >= 32
        assert account['token'] not in first.stdout + second.stdout + json.dumps(stored)
    assert len(stored) == 4 and {row['role'] for row in stored} == {'user','operator','admin'}
