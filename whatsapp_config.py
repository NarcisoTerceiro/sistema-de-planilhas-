"""Guarda apenas a publicação no PostgreSQL externo. Conversas seguem temporárias."""
import base64
import json
import os
from pathlib import Path


def configured():
    if os.environ.get('WHATSAPP_CACHE_ONLY', '').lower() in ('true', '1', 'yes'):
        return False
    return bool(os.environ.get('WHATSAPP_CONFIG_DATABASE_URL', '').strip())


def connection():
    import psycopg2
    conn = psycopg2.connect(os.environ['WHATSAPP_CONFIG_DATABASE_URL'], connect_timeout=15)
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS df_whatsapp_publication (id TEXT PRIMARY KEY, payload JSONB NOT NULL)')
    conn.commit()
    return conn


def save(root, config):
    if not configured():
        return
    folder = Path(root) / 'snapshots' / config['revision']
    paths = [folder / 'workspace.yaml', *sorted((folder / 'data').glob('*.parquet'))]
    if sum(p.stat().st_size for p in paths) > 25 * 1024 * 1024:
        raise ValueError('A publicação ultrapassa o limite de 25 MB desta versão de testes.')
    payload = {'config': config, 'files': {p.relative_to(folder).as_posix(): base64.b64encode(p.read_bytes()).decode() for p in paths}}
    conn = connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute('INSERT INTO df_whatsapp_publication(id,payload) VALUES(%s,%s::jsonb) ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload',
                        (os.environ.get('WHATSAPP_CONFIG_ID', 'principal'), json.dumps(payload)))
    finally:
        conn.close()


def restore(root):
    if not configured():
        return False
    conn = connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT payload FROM df_whatsapp_publication WHERE id=%s', (os.environ.get('WHATSAPP_CONFIG_ID', 'principal'),))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return False
    payload = row[0]
    config = payload['config']
    import re
    if not re.fullmatch(r'[0-9a-f]{32}', config['revision']):
        raise ValueError('Publicação persistente inválida.')
    folder = Path(root) / 'snapshots' / config['revision']
    for filename, content in payload['files'].items():
        path = Path(filename)
        if filename != 'workspace.yaml' and not (len(path.parts) == 2 and path.parts[0] == 'data' and path.suffix == '.parquet'):
            raise ValueError('Arquivo inesperado na publicação.')
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Caminho inválido na publicação.')
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(content, validate=True))
    root = Path(root)
    temporary = root / 'config-restored.tmp'
    temporary.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, root / 'config.json')
    return True
