"""Register an embedding space or process durable indexing jobs (local only)."""
import argparse
import json
import os
import time

from embeddings import EmbeddingError, HashEmbedder, OllamaEmbedder
from knowledge import KnowledgeStore, run_once
from settings import load_env
from storage import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['register-space', 'once', 'run'])
    parser.add_argument('--backend', choices=['hash', 'ollama'], default='hash')
    parser.add_argument('--model', default='nomic-embed-text:v1.5')
    parser.add_argument('--no-env', action='store_true', help='Use explicit process environment, ignoring local .env')
    args = parser.parse_args()
    if not args.no_env:
        load_env()
    db = Database(os.getenv('DATABASE_URL', 'fielddesk.db'))
    db.initialize()
    store = KnowledgeStore(db)
    try:
        adapter = HashEmbedder() if args.backend == 'hash' else OllamaEmbedder(args.model)
    except EmbeddingError as failure:
        print(json.dumps({'status': 'unavailable', 'error': str(failure)}))
        return 1
    store.register_space(adapter.space)
    if args.command == 'register-space':
        print(json.dumps({'space_id': adapter.space.id, 'model': adapter.space.model,
                          'digest': adapter.space.digest, 'dimensions': adapter.space.dimensions}))
        return 0
    try:
        while True:
            result = run_once(store, adapter)
            if result:
                print(json.dumps(result), flush=True)
            if args.command == 'once':
                return 0 if not result or result['outcome'] == 'ready' else 1
            if not result:
                time.sleep(1)
    except KeyboardInterrupt:
        return 0  # Interrupted jobs remain leased and are reclaimed after expiry.


if __name__ == '__main__':
    raise SystemExit(main())
