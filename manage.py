"""Generate local-only keys or load synthetic manuals; never prints credentials."""
import argparse
import json
import secrets
from pathlib import Path
from app import create_app, embedding
import hashlib
from settings import load_env

load_env()

parser = argparse.ArgumentParser()
parser.add_argument('command', choices=['keys', 'seed'])
args = parser.parse_args()
if args.command == 'keys':
    with Path('.env').open('x', encoding='utf-8') as f:
        f.write('FIELDDESK_USER_KEY=' + secrets.token_urlsafe(32) + '\n')
        f.write('FIELDDESK_OPERATOR_KEY=' + secrets.token_urlsafe(32) + '\n')
        f.write('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24) + '\n')
    print('Created .env (ignored). Load it into your environment; do not publish it.')
else:
    app = create_app()
    db = app.state.db
    with db.connect() as c:
        for doc in json.loads(Path('fixtures/manuals.json').read_text(encoding='utf-8')):
            doc_id = hashlib.sha256((doc['title'] + '\0' + doc['body']).encode()).hexdigest()
            db.run(c, 'INSERT INTO documents VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING',
                   (doc_id, doc['title'], doc['body'], json.dumps(embedding(doc['body']))))
    print('Synthetic manuals loaded.')
