import json
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from app import create_app
from embeddings import EmbeddingError, HashEmbedder, OllamaEmbedder, Space
from knowledge import KnowledgeStore, chunks_for, run_once
from storage import Database

OPERATOR = {'Authorization': 'Bearer fixture-operator'}
USER = {'Authorization': 'Bearer fixture-user'}


@pytest.fixture
def store(tmp_path):
    url = os.getenv('TEST_DATABASE_URL', str(tmp_path / 'knowledge.db'))
    db = Database(url)
    if db.postgres:
        from psycopg.conninfo import conninfo_to_dict
        if not conninfo_to_dict(url)['dbname'].endswith('_test'):
            raise ValueError('Knowledge integration checks require a disposable *_test database')
    db.initialize()
    if db.postgres:
        with db.connect() as conn:
            conn.execute('TRUNCATE rag_traces,document_grants,document_permissions,rag_identities,index_jobs,chunk_embeddings,document_chunks,index_targets,document_versions,knowledge_documents,embedding_spaces')
    clock = [100.0]
    result = KnowledgeStore(db, clock=lambda: clock[0])
    result.test_clock = clock
    return result


def count(store, table):
    allowed = {'document_versions', 'index_jobs', 'document_chunks', 'chunk_embeddings'}
    assert table in allowed
    with store.db.connect() as conn:
        return store.db.run(conn, f'SELECT count(*) AS n FROM {table}').fetchone()['n']


def test_chunk_offsets_are_deterministic_unicode_characters_with_bounded_overlap():
    body = 'Campus café 🔧 equipment.\n' * 100
    chunks = chunks_for('version', body)
    assert chunks == chunks_for('version', body)
    assert chunks[0]['start_offset'] == 0 and chunks[-1]['end_offset'] == len(body)
    for i, chunk in enumerate(chunks):
        assert chunk['text'] == body[chunk['start_offset']:chunk['end_offset']]
        assert len(chunk['text']) <= 600
        if i:
            assert chunk['start_offset'] == chunks[i-1]['end_offset'] - 80


def test_ingest_replay_and_successful_worker_are_durable_without_duplicate_rows(store):
    job = store.ingest('projector', 'Repairs', 'Projector repair approval. ' * 50)
    again = store.ingest('projector', 'Repairs', 'Projector repair approval. ' * 50)
    assert job['id'] == again['id'] and job['status'] == 'pending'
    assert count(store, 'document_versions') == count(store, 'index_jobs') == 1
    assert run_once(store, HashEmbedder()) == {'job_id': job['id'], 'outcome': 'ready'}
    restarted = KnowledgeStore(Database(store.db.url), clock=store.clock)
    restarted.db.initialize()
    assert restarted.job(job['id'])['status'] == 'ready'
    ready = restarted.ready_chunks(job['version_id'], job['space_id'])
    assert len(ready) == 3
    assert count(store, 'document_chunks') == count(store, 'chunk_embeddings') == 3
    assert run_once(restarted, HashEmbedder()) is None
    assert restarted.ingest('projector', 'Repairs', 'Projector repair approval. ' * 50)['status'] == 'ready'
    assert count(store, 'index_jobs') == 1


def test_version_replacement_keeps_old_ready_until_atomic_publish_and_old_replay_does_not_rollback(store):
    first = store.ingest('manual', 'Version one', 'First approved campus instructions.')
    run_once(store, HashEmbedder())
    second = store.ingest('manual', 'Version two', 'Replacement approved campus instructions.')
    document = store.document(first['document_id'])
    assert document['indexes'][0]['ready_version_id'] == first['version_id']
    assert document['indexes'][0]['desired_version_id'] == second['version_id']
    assert store.ready_chunks(second['version_id'], second['space_id']) is None
    store.ingest('manual', 'Version one', 'First approved campus instructions.')
    assert store.document(first['document_id'])['indexes'][0]['desired_version_id'] == second['version_id']
    run_once(store, HashEmbedder())
    assert store.document(first['document_id'])['indexes'][0]['ready_version_id'] == second['version_id']
    assert len(store.document(first['document_id'])['versions']) == 2


def test_late_old_version_worker_cannot_publish_over_new_desired_version(store):
    first = store.ingest('manual', 'Old', 'Old instructions.')
    old_claim = store.claim(first['space_id'])
    second = store.ingest('manual', 'New', 'New instructions.')
    run_once(store, HashEmbedder())
    old_chunks = chunks_for(first['version_id'], old_claim['version']['body'])
    assert store.publish(old_claim, HashEmbedder.space, old_chunks, HashEmbedder().embed([c['text'] for c in old_chunks]))
    assert store.document(first['document_id'])['indexes'][0]['ready_version_id'] == second['version_id']


def test_crashed_worker_lease_is_reclaimed_by_a_new_instance(store):
    job = store.ingest('manual', 'Manual', 'Durable campus equipment instructions.')
    abandoned = store.claim(job['space_id'], lease_seconds=5)
    assert store.job(job['id'])['attempts'] == 1
    assert run_once(store, HashEmbedder()) is None
    store.test_clock[0] += 6
    restarted = KnowledgeStore(Database(store.db.url), clock=store.clock)
    assert run_once(restarted, HashEmbedder())['outcome'] == 'ready'
    assert store.job(job['id'])['attempts'] == 2
    chunks = chunks_for(job['version_id'], abandoned['version']['body'])
    assert not store.publish(abandoned, HashEmbedder.space, chunks, HashEmbedder().embed([c['text'] for c in chunks]))
    assert count(store, 'chunk_embeddings') == 1


def test_expired_worker_cannot_publish_even_before_someone_reclaims_it(store):
    job = store.ingest('manual', 'Manual', 'Lease fencing must hold.')
    claim = store.claim(job['space_id'], lease_seconds=1)
    store.test_clock[0] += 2
    chunks = chunks_for(job['version_id'], claim['version']['body'])
    assert not store.publish(claim, HashEmbedder.space, chunks, HashEmbedder().embed([c['text'] for c in chunks]))
    assert count(store, 'document_chunks') == 0


def test_failure_before_publish_is_atomic_and_retries_are_bounded_without_error_text_leak(store):
    class Broken(HashEmbedder):
        def embed(self, texts):
            raise RuntimeError('private document or credentials must not reach status')
    job = store.ingest('manual', 'Manual', 'Synthetic text. ' * 800)
    for attempt in range(1, 4):
        assert run_once(store, Broken())['outcome'] == 'retry_or_failed'
        status = store.job(job['id'])
        assert status['attempts'] == attempt and status['last_error'] == 'embedding_failed'
        assert status['status'] == ('failed' if attempt == 3 else 'pending')
        assert count(store, 'document_chunks') == count(store, 'chunk_embeddings') == 0
        store.test_clock[0] += 10
    assert run_once(store, HashEmbedder()) is None


def test_partial_embedding_batches_never_publish_and_valid_retry_recovers(store):
    class Partial(HashEmbedder):
        calls = 0
        def embed(self, texts):
            self.calls += 1
            return super().embed(texts) if self.calls == 1 else [[float('nan')] * 256 for _ in texts]
    job = store.ingest('manual', 'Manual', 'Synthetic campus text. ' * 400)
    assert run_once(store, Partial())['outcome'] == 'retry_or_failed'
    assert store.job(job['id'])['last_error'] == 'invalid_embedding'
    assert count(store, 'document_chunks') == count(store, 'chunk_embeddings') == 0
    store.test_clock[0] += 3
    assert run_once(store, HashEmbedder())['outcome'] == 'ready'
    assert store.job(job['id'])['indexed_chunks'] > 8


def test_expired_final_attempt_is_failed_instead_of_remaining_running_forever(store):
    job = store.ingest('manual', 'Manual', 'Attempts have a bound.')
    for _ in range(3):
        assert store.claim(job['space_id'], lease_seconds=1)
        store.test_clock[0] += 2
    assert store.claim(job['space_id']) is None
    assert store.job(job['id'])['status'] == 'failed'
    assert store.job(job['id'])['last_error'] == 'lease_expired'


def test_publication_database_failure_rolls_back_all_new_rows_and_keeps_previous_ready(store, monkeypatch):
    first = store.ingest('manual', 'First', 'Previously approved equipment instructions.')
    run_once(store, HashEmbedder())
    replacement = store.ingest('manual', 'Replacement', 'New equipment instruction. ' * 70)
    original = store.db.run
    inserts = [0]
    def fail_second_vector(conn, sql, params=()):
        if sql.startswith('INSERT INTO chunk_embeddings'):
            inserts[0] += 1
            if inserts[0] == 2:
                raise RuntimeError('Injected database publication failure')
        return original(conn, sql, params)
    monkeypatch.setattr(store.db, 'run', fail_second_vector)
    assert run_once(store, HashEmbedder())['outcome'] == 'retry_or_failed'
    assert count(store, 'document_chunks') == count(store, 'chunk_embeddings') == 1
    assert store.job(replacement['id'])['indexed_chunks'] == 0
    assert store.document(first['document_id'])['indexes'][0]['ready_version_id'] == first['version_id']
    monkeypatch.setattr(store.db, 'run', original)
    store.test_clock[0] += 3
    assert run_once(store, HashEmbedder())['outcome'] == 'ready'
    assert store.document(first['document_id'])['indexes'][0]['ready_version_id'] == replacement['version_id']


def test_concurrent_workers_claim_one_job_once(store):
    job = store.ingest('manual', 'Manual', 'One publication under worker contention.')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run_once(store, HashEmbedder()), range(4)))
    assert sum(result is not None for result in results) == 1
    assert store.job(job['id'])['attempts'] == 1
    assert count(store, 'document_chunks') == count(store, 'chunk_embeddings') == 1


def test_embedding_spaces_are_dimension_bound_and_never_reuse_hash_vectors(store):
    class SyntheticEmbedder:
        # Deterministic adapter fixture, not evidence of a real learned model.
        space = Space('test', 'fixture-model', 'fixture-digest', 3)
        def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]
    hash_job = store.ingest('manual', 'Manual', 'Separate embedding spaces.')
    fake = SyntheticEmbedder()
    store.register_space(fake.space)
    other_job = store.ingest('manual', 'Manual', 'Separate embedding spaces.', fake.space.id)
    assert hash_job['version_id'] == other_job['version_id']
    assert hash_job['space_id'] != other_job['space_id']
    run_once(store, HashEmbedder())
    run_once(store, fake)
    assert store.job(hash_job['id'])['dimensions'] == 256
    assert store.job(other_job['id'])['dimensions'] == 3
    assert count(store, 'document_chunks') == 1 and count(store, 'chunk_embeddings') == 2
    if store.db.postgres:
        from psycopg import IntegrityError
    else:
        from sqlite3 import IntegrityError
    with pytest.raises(IntegrityError):
        with store.db.connect() as conn:
            store.db.run(conn, 'UPDATE chunk_embeddings SET dimensions=3 WHERE space_id=?', (hash_job['space_id'],))


def test_new_operator_api_enqueues_reports_status_and_exposes_exact_ready_spans(store):
    app = create_app(store.db.url, 'fixture-user', 'fixture-operator')
    with TestClient(app) as client:
        payload = {'source_key': 'projector', 'title': 'Repairs', 'body': ' \nUse an approved equipment ticket.\n '}
        assert client.post('/knowledge/documents', headers=USER, json=payload).status_code == 403
        response = client.post('/knowledge/documents', headers=OPERATOR, json=payload)
        assert response.status_code == 202
        job = response.json()
        # API store uses real time; use its clock for the worker in this API scenario.
        assert 'lease_token' not in job and job['status'] == 'pending'
        run_once(app.state.knowledge, HashEmbedder())
        assert client.get('/knowledge/jobs/' + job['id'], headers=OPERATOR).json()['status'] == 'ready'
        path = f"/knowledge/versions/{job['version_id']}/chunks?space_id={job['space_id']}"
        ready = client.get(path, headers=OPERATOR).json()
        assert ready[0]['text'] == payload['body']
        assert ready[0]['end_offset'] == len(payload['body'])
        for endpoint in ['/knowledge/spaces', '/knowledge/jobs/' + job['id'],
                         '/knowledge/documents/' + job['document_id'], path]:
            assert client.get(endpoint).status_code == 401
            assert client.get(endpoint, headers=USER).status_code == 403
        assert client.get('/knowledge/jobs/missing', headers=OPERATOR).status_code == 404
        assert client.get('/knowledge/documents/missing', headers=OPERATOR).status_code == 404
        assert client.post('/knowledge/documents', headers=OPERATOR, json={**payload, 'space_id': 'f'*64}).status_code == 422
        assert client.post('/knowledge/documents', headers=OPERATOR, json={**payload, 'body': 'bad\0text'}).status_code == 422


def test_adopts_legacy_unversioned_database_without_losing_existing_rows(tmp_path):
    db = Database(str(tmp_path / 'legacy.db'))
    from storage import migrations
    with db.connect() as conn:
        for statement in migrations(False)[0][1]:
            conn.execute(statement)
        conn.execute('INSERT INTO documents VALUES (?,?,?,?)', ('legacy', 'Manual', 'Old body', json.dumps([0]*256)))
        conn.execute('INSERT INTO tickets VALUES (?,?,?)', ('ticket', 'Approved before upgrade', 'open'))
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        assert conn.execute('SELECT body FROM documents').fetchone()['body'] == 'Old body'
        assert conn.execute('SELECT title FROM tickets').fetchone()['title'] == 'Approved before upgrade'
        assert conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 3
        conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=2")
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        db.initialize()


def test_migration_initialization_serializes_concurrent_startups(tmp_path):
    path = str(tmp_path / 'startup.db')
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: Database(path).initialize(), range(3)))
    with Database(path).connect() as conn:
        assert conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 3


def test_legacy_upgrade_preserves_documents_and_tickets_on_selected_backend(store):
    # The fixture owns a disposable *_test DB. Remove only the new schema to
    # reproduce the previously unversioned four-table application layout.
    with store.db.connect() as conn:
        for table in ['rag_traces', 'document_grants', 'document_permissions', 'rag_identities',
                      'index_jobs', 'chunk_embeddings', 'document_chunks', 'index_targets',
                      'document_versions', 'knowledge_documents', 'embedding_spaces', 'schema_migrations']:
            conn.execute('DROP TABLE ' + table)
        store.db.run(conn, 'INSERT INTO documents VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING',
                     ('legacy-upgrade', 'Legacy manual', 'Original approved text', json.dumps([0]*256)))
        store.db.run(conn, 'INSERT INTO tickets VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',
                     ('legacy-upgrade-ticket', 'Original approved repair', 'open'))
    store.db.initialize()
    store.db.initialize()
    with store.db.connect() as conn:
        assert store.db.run(conn, "SELECT body FROM documents WHERE id='legacy-upgrade'").fetchone()['body'] == 'Original approved text'
        assert store.db.run(conn, "SELECT title FROM tickets WHERE id='legacy-upgrade-ticket'").fetchone()['title'] == 'Original approved repair'


def test_ollama_adapter_checks_dimensions_digest_and_uses_no_silent_truncation(monkeypatch):
    calls = []
    def respond(self, path, payload=None):
        calls.append((path, payload))
        return {'models': [{'name': 'test-model', 'digest': 'fixed-digest'}]} if path == '/api/tags' else {'embeddings': [[1.0, 0.0, 0.0] for _ in payload['input']]}
    monkeypatch.setattr(OllamaEmbedder, '_request', respond)
    adapter = OllamaEmbedder('test-model')
    assert adapter.space.dimensions == 3
    assert adapter.embed(['Synthetic text']) == [[1.0, 0.0, 0.0]]
    assert calls[-2][1]['truncate'] is False
    assert calls[-2][1]['input'] == ['search_document: Synthetic text']
    with pytest.raises(ValueError):
        adapter.embed(['x' * 601])
    monkeypatch.setattr(adapter, '_digest', lambda: 'changed-digest')
    with pytest.raises(EmbeddingError, match='model_changed'):
        adapter.embed(['Synthetic text'])
    with pytest.raises(ValueError, match='loopback'):
        OllamaEmbedder(base_url='https://external.invalid')
