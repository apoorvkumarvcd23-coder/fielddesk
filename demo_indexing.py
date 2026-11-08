"""Isolated synthetic API -> crashed worker -> restart -> ready-version demo."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app import create_app
from embeddings import HashEmbedder, OllamaEmbedder

ROOT = Path(__file__).resolve().parent
OPERATOR = {'Authorization': 'Bearer synthetic-demo-operator'}


@contextmanager
def disposable_postgres(database_name='fielddesk_demo'):
    """Own one uniquely named container; never attach to a configured user DB."""
    import psycopg
    if database_name not in {'fielddesk_demo', 'fielddesk_test'}:
        raise ValueError('Only isolated demo/test database names are allowed')
    name = 'fielddesk-index-demo-' + uuid4().hex[:12]
    password = secrets.token_hex(24)
    subprocess.run(['docker', 'run', '--rm', '-d', '--name', name,
                    '-e', 'POSTGRES_USER=fielddesk', '-e', 'POSTGRES_DB=' + database_name,
                    '-e', 'POSTGRES_PASSWORD', '-p', '127.0.0.1::5432',
                    '--tmpfs', '/var/lib/postgresql/data', 'pgvector/pgvector:pg17'],
                   env={**os.environ, 'POSTGRES_PASSWORD': password}, check=True, capture_output=True, text=True)
    try:
        port = subprocess.check_output(['docker', 'port', name, '5432/tcp'], text=True).strip().rsplit(':', 1)[1]
        url = f'postgresql://fielddesk:{password}@127.0.0.1:{port}/{database_name}'
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(url, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Disposable PostgreSQL did not become ready') from None
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(['docker', 'stop', name], check=True, capture_output=True, text=True)


def demonstrate(url, backend='hash'):
    adapter = HashEmbedder() if backend == 'hash' else OllamaEmbedder()
    app = create_app(url, 'synthetic-demo-user', 'synthetic-demo-operator')
    store = app.state.knowledge
    store.register_space(adapter.space)
    environment = {**os.environ, 'DATABASE_URL': url, 'DEMO_SPACE_ID': adapter.space.id}
    # Keep model/DB URLs and all credentials out of the report and subprocess logs.
    worker = [sys.executable, str(ROOT / 'index_worker.py'), 'once', '--backend', backend, '--no-env']
    with TestClient(app) as client:
        payload = {'source_key': 'synthetic-projector-manual', 'title': 'Synthetic equipment manual',
                   'body': 'Projector repairs require an approved operator ticket. ' * 30,
                   'space_id': adapter.space.id}
        response = client.post('/knowledge/documents', headers=OPERATOR, json=payload)
        response.raise_for_status()
        job = response.json()
        assert job['status'] == 'pending'
        crash_code = (
            "import os; from storage import Database; from knowledge import KnowledgeStore; "
            "s=KnowledgeStore(Database(os.environ['DATABASE_URL'])); "
            "j=s.claim(os.environ['DEMO_SPACE_ID'],lease_seconds=1); "
            "assert j is not None; os._exit(23)"
        )
        crashed = subprocess.run([sys.executable, '-c', crash_code], cwd=ROOT, env=environment,
                                 capture_output=True, text=True, timeout=30)
        assert crashed.returncode == 23
        assert store.job(job['id'])['status'] == 'running'
        time.sleep(1.2)
        restarted = subprocess.run(worker, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=180)
        if restarted.returncode != 0:
            raise RuntimeError('Restarted index worker failed; inspect local setup, no source text logged')
        ready = client.get('/knowledge/jobs/' + job['id'], headers=OPERATOR).json()
        assert ready['status'] == 'ready' and ready['attempts'] == 2
        rows = client.get(f"/knowledge/versions/{job['version_id']}/chunks?space_id={adapter.space.id}", headers=OPERATOR).json()
        assert len(rows) == ready['indexed_chunks']
        assert all(row['text'] == payload['body'][row['start_offset']:row['end_offset']] for row in rows)
        replay = client.post('/knowledge/documents', headers=OPERATOR, json=payload).json()
        assert replay['id'] == ready['id'] and replay['indexed_chunks'] == len(rows)
        replacement_payload = {**payload, 'body': 'Updated synthetic policy: ' + payload['body']}
        replacement = client.post('/knowledge/documents', headers=OPERATOR, json=replacement_payload).json()
        assert replacement['version_id'] != ready['version_id']
        assert store.document(job['document_id'])['indexes'][0]['ready_version_id'] == ready['version_id']
        completed = subprocess.run(worker, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=180)
        if completed.returncode != 0:
            raise RuntimeError('Replacement index worker failed')
        assert store.document(job['document_id'])['indexes'][0]['ready_version_id'] == replacement['version_id']
    with store.db.connect() as conn:
        if store.db.postgres:
            dimensions = store.db.run(conn, '''SELECT min(vector_dims(ce.embedding)) AS low,max(vector_dims(ce.embedding)) AS high
                FROM chunk_embeddings ce WHERE ce.space_id=?''', (adapter.space.id,)).fetchone()
            server = conn.execute('SHOW server_version').fetchone()['server_version']
        else:
            dimensions = store.db.run(conn, '''SELECT min(json_array_length(embedding)) AS low,
                max(json_array_length(embedding)) AS high FROM chunk_embeddings WHERE space_id=?''', (adapter.space.id,)).fetchone()
            server = 'SQLite'
    assert dimensions['low'] == dimensions['high'] == adapter.space.dimensions
    return {
        'scope': 'Indexing foundation only; chat still uses the separate offline hash corpus',
        'storage': 'PostgreSQL/pgvector' if store.db.postgres else 'SQLite', 'server_version': server,
        'backend': backend, 'model': adapter.space.model, 'digest': adapter.space.digest,
        'space_id': adapter.space.id, 'dimensions': adapter.space.dimensions,
        'stored_dimensions_min_max': [dimensions['low'], dimensions['high']],
        'first_job': ready, 'worker_crash_exit_code': crashed.returncode,
        'restarted_worker_completed': True, 'exact_replay_no_duplicates': True,
        'source_offsets_verified': True, 'previous_ready_retained_until_replacement': True,
        'replacement_job': store.job(replacement['id']),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['hash', 'ollama'], default='hash')
    parser.add_argument('--postgres', action='store_true', help='Start/stop a new isolated Docker database')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.postgres:
        with disposable_postgres() as url:
            report = demonstrate(url, args.backend)
    else:
        with TemporaryDirectory(prefix='fielddesk-index-demo-') as directory:
            report = demonstrate(str(Path(directory) / 'demo.db'), args.backend)
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')


if __name__ == '__main__':
    main()
