"""Actual local-model synthetic RAG acceptance; no .env or personal database reads."""
import argparse
from contextlib import nullcontext
from dataclasses import asdict
import json
from pathlib import Path
import platform
import secrets
import socket
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app import create_app
from demo_indexing import disposable_postgres
from embeddings import OllamaEmbedder
from generation import OllamaGenerator
from knowledge import run_once
from rag import add_identity, set_grant


def demonstrate(url, generation_model):
    embedder = OllamaEmbedder()
    generator = OllamaGenerator(generation_model)
    app = create_app(url, secrets.token_urlsafe(32), secrets.token_urlsafe(32), embedder, generator)
    store, db = app.state.knowledge, app.state.db
    store.register_space(embedder.space)
    actors = {}
    for name, role in [('alice','user'), ('bob','user'), ('operator','operator'), ('admin','admin')]:
        token = secrets.token_urlsafe(32)
        actors[name] = {'id': add_identity(db, name, role, token),
                        'headers': {'Authorization': 'Bearer ' + token}}
    jobs = {}
    for manual in json.loads((Path(__file__).parent / 'fixtures/rag_manuals.json').read_text()):
        job = store.ingest(manual['source_key'], manual['title'], manual['body'], embedder.space.id)
        jobs[manual['source_key']] = job
        for reader in manual['readers']:
            set_grant(db, job['document_id'], actors[reader]['id'], True)
    while result := run_once(store, embedder):
        assert result['outcome'] == 'ready', result['outcome']
    seen = []
    original = generator.generate
    def record(question, evidence):
        seen.extend(r['document_id'] for r in evidence)
        return original(question, evidence)
    generator.generate = record
    results = []
    with TestClient(app) as client:
        def ask(label, question, expected_handoff):
            response = client.post('/rag/chat', headers=actors['alice']['headers'], json={'message':question})
            response.raise_for_status()
            result = response.json()
            trace = client.get('/rag/traces/' + result['trace_id'], headers=actors['alice']['headers']).json()
            assert result['handoff'] is expected_handoff, (label, result)
            if not result['handoff']:
                assert result['citations']
                for cite in result['citations']:
                    source = client.get('/rag/chunks/' + cite['chunk_id'],
                        params={'space_id':cite['space_id']}, headers=actors['alice']['headers'])
                    source.raise_for_status()
                    assert cite['quote'] in source.json()['text']
            results.append({'case':label, 'question':question, 'response':result,
                            'elapsed_ms':trace['elapsed_ms'], 'metadata':json.loads(trace['metadata'])})
            return result
        supported = ask('answerable', 'What must I record in a projector repair ticket?', False)
        ask('unsupported', 'What is the price of a projector repair?', True)
        ask('restricted', 'What is the restricted lab access code?', True)
        private = jobs['synthetic-restricted-access']
        private_chunk = store.ready_chunks(private['version_id'], private['space_id'])[0]['id']
        denied = client.get('/rag/chunks/' + private_chunk, params={'space_id':private['space_id']},
                            headers=actors['alice']['headers']).status_code
        assert denied == 404 and private['document_id'] not in seen
        # Actual failed loopback connection, without interrupting a shared Ollama server.
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))  # bound but not listening
            original_url = generator.base_url
            generator.base_url = f'http://127.0.0.1:{reserved.getsockname()[1]}'
            try:
                ask('generation_endpoint_unavailable', 'What must I record in a projector repair ticket?', True)
            finally:
                generator.base_url = original_url
        previous_timeout = generator.timeout
        generator.timeout = 0.001  # actual local Ollama transport deadline, not a mock adapter
        try:
            ask('actual_transport_timeout_1ms', 'What must I record in a projector repair ticket?', True)
        finally:
            generator.timeout = previous_timeout
        doc = jobs['synthetic-projector-policy']['document_id']
        set_grant(db, doc, actors['alice']['id'], False)
        ask('grant_revoked', 'What must I record in a projector repair ticket?', True)
        cite = supported['citations'][0]
        assert client.get('/rag/chunks/' + cite['chunk_id'], params={'space_id':cite['space_id']},
                          headers=actors['alice']['headers']).status_code == 404
    return {
        'inference': 'actual Ollama embedding and generation requests, no mocked model adapters',
        'storage': 'PostgreSQL/pgvector' if db.postgres else 'SQLite exact scan (not pgvector)',
        'platform': platform.system(), 'python': platform.python_version(),
        'embedding': asdict(embedder.space),
        'generation': {'model':generator.model, 'digest':generator.digest,
                       'num_ctx':4096,'num_predict':400,'temperature':0,'seed':7,'think':False},
        'indexed_documents': len(jobs), 'restricted_source_http_status':denied,
        'restricted_document_excluded_before_prompt': private['document_id'] not in seen,
        'cases':results,
        'limits':'Synthetic demonstration, not a held-out quality score or full core release. '
                 'Citation ID/quote matching does not prove semantic support; inspect the actual answer.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--generation-model', default='qwen3.5:9b')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='fielddesk-rag-') as temporary:
        context = disposable_postgres() if args.postgres else nullcontext(str(Path(temporary) / 'demo.db'))
        with context as url:
            report = demonstrate(url, args.generation_model)
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
