import os
from pathlib import Path


def load_env():
    path = Path('.env')
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            key, _, value = line.partition('=')
            if key in {'FIELDDESK_USER_KEY', 'FIELDDESK_OPERATOR_KEY', 'DATABASE_URL', 'POSTGRES_PASSWORD'}:
                os.environ.setdefault(key, value)
