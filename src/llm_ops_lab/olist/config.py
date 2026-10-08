"""Local connection configuration; secrets never belong in CLI output or receipts."""

import os
from pathlib import Path

from psycopg.conninfo import make_conninfo


def connection_info(root: Path, *, reader: bool = False) -> str:
    role = 'reader' if reader else 'writer'
    override = os.environ.get(f'OLIST_{role.upper()}_DSN')
    if override:
        return override
    secret = root / 'work/olist/secrets' / f'{role}.txt'
    return make_conninfo(
        host='127.0.0.1', port=os.environ.get('OLIST_PG_PORT', '55432'),
        dbname='olist', user=f'olist_{role}', password=secret.read_text().strip(),
        connect_timeout=5, application_name=f'llm-ops-olist-{role}',
    )
