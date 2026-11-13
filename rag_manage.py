"""Generate ignored identity credentials; seed only synthetic RAG notes/grants."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets

from embeddings import OllamaEmbedder
from knowledge import KnowledgeStore
from rag import add_identity, set_grant
from settings import load_env
from storage import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['identities', 'seed', 'purge-reviews'])
    args = parser.parse_args()
    load_env()
    db = Database(os.getenv('DATABASE_URL', 'fielddesk.db'))
    db.initialize()
    if args.command == 'purge-reviews':
        from reviews import ReviewStore
        print(json.dumps({'redacted_expired_reviews': ReviewStore(db).purge()}))
        return
    credentials = Path('credentials.json')
    if args.command == 'identities':
        roles = dict([
            ('alice', 'user'), ('bob', 'user'), ('operator', 'operator'), ('admin', 'admin')
        ])
        if credentials.exists():
            accounts = json.loads(credentials.read_text(encoding='utf-8'))
            if set(accounts) != set(roles) or any(
                account.get('role') != roles[name] or not isinstance(account.get('token'), str)
                or len(account['token']) < 24 for name, account in accounts.items()
            ):
                raise SystemExit('Existing credentials.json is incompatible and was preserved')
        else:
            accounts = {name: {'role': role, 'token': secrets.token_urlsafe(32)} for name, role in roles.items()}
            fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(accounts, output, indent=2)
        for name, account in accounts.items():
            account['id'] = add_identity(db, name, account['role'], account['token'])
            with db.connect() as conn:
                row = db.run(conn, 'SELECT token_hash,role FROM rag_identities WHERE id=?', (account['id'],)).fetchone()
                if row['token_hash'] != hashlib.sha256(account['token'].encode()).hexdigest() or row['role'] != account['role']:
                    raise SystemExit('Identity file/database mismatch; existing credentials were preserved')
        credentials.write_text(json.dumps(accounts, indent=2) + '\n', encoding='utf-8')
        print('Created ignored credentials.json; bearer tokens are never printed. Keep it private.')
    else:
        accounts = json.loads(credentials.read_text(encoding='utf-8'))
        for name, account in accounts.items():
            with db.connect() as conn:
                found = db.run(conn, 'SELECT token_hash FROM rag_identities WHERE id=?', (account['id'],)).fetchone()
                if not found or found['token_hash'] != hashlib.sha256(account['token'].encode()).hexdigest():
                    raise SystemExit('Identity file/database mismatch; no notes were seeded')
        adapter = OllamaEmbedder()
        knowledge = KnowledgeStore(db)
        knowledge.register_space(adapter.space)
        manuals = json.loads((Path(__file__).parent / 'fixtures/rag_manuals.json').read_text())
        for manual in manuals:
            job = knowledge.ingest(manual['source_key'], manual['title'], manual['body'], adapter.space.id)
            for name in manual['readers']:
                set_grant(db, job['document_id'], accounts[name]['id'], True)
        print('Synthetic notes queued in the learned space. Run the Ollama index worker.')


if __name__ == '__main__':
    main()
