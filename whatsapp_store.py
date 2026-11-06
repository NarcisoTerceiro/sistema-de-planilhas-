"""Fila persistente e memória isolada por conversa. Um worker por serviço."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, sender TEXT NOT NULL, body TEXT NOT NULL,
                received REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT);
              CREATE TABLE IF NOT EXISTS conversations (
                key TEXT PRIMARY KEY, revision TEXT NOT NULL, history TEXT NOT NULL, updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS outbox (
                job_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', meta_id TEXT, error TEXT,
                PRIMARY KEY(job_id, seq));
            ''')
            # Migração aditiva: colunas de mídia para enviar PDFs (gráficos) na fila.
            existing = {row['name'] for row in db.execute('PRAGMA table_info(outbox)')}
            if 'media_path' not in existing:
                db.execute('ALTER TABLE outbox ADD COLUMN media_path TEXT')
            if 'filename' not in existing:
                db.execute('ALTER TABLE outbox ADD COLUMN filename TEXT')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Já há resposta materializada? Retome o envio, não rode a IA de novo.
            db.execute("UPDATE jobs SET status='ready' WHERE status IN ('processing','delivering','queued') AND id IN (SELECT job_id FROM outbox)")
            db.execute("UPDATE jobs SET status='queued' WHERE status='processing'")
            # Uma queda entre envio e confirmação é ambígua. Não reenviar automaticamente.
            db.execute("UPDATE outbox SET status='uncertain', error='Envio sem confirmação após reinício' WHERE status='sending'")
            db.execute("UPDATE jobs SET status='uncertain' WHERE id IN (SELECT job_id FROM outbox WHERE status='uncertain')")

    def enqueue(self, items, max_pending=100):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            unique = {item['id']: item for item in items}
            new = [item for item in unique.values() if not db.execute('SELECT 1 FROM jobs WHERE id=?', (item['id'],)).fetchone()]
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing','ready','delivering')").fetchone()[0]
            if pending + len(new) > max_pending:
                raise OverflowError('Fila cheia')
            for item in new:
                db.execute('INSERT OR IGNORE INTO jobs(id,sender,body,received) VALUES(?,?,?,?)',
                           (item['id'], item['sender'], item['body'], item['received']))
        return len(new)

    def next_job(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status IN ('queued','ready') ORDER BY received,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            job = dict(row)
            if job['status'] == 'queued':
                db.execute("UPDATE jobs SET status='processing' WHERE id=?", (job['id'],))
            else:
                db.execute("UPDATE jobs SET status='delivering' WHERE id=?", (job['id'],))
            return job

    def history(self, key, revision, ttl=86400):
        with self.db() as db:
            row = db.execute('SELECT * FROM conversations WHERE key=?', (key,)).fetchone()
        if not row or row['revision'] != revision or time.time() - row['updated'] > ttl:
            return []
        return json.loads(row['history'])

    def conversation_revision(self, key):
        with self.db() as db:
            row = db.execute('SELECT revision,updated FROM conversations WHERE key=?', (key,)).fetchone()
        return dict(row) if row else None

    def save_history(self, key, revision, history):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO conversations VALUES(?,?,?,?)',
                       (key, revision, json.dumps(history, ensure_ascii=False), time.time()))

    def ready(self, job_id, chunks, key, revision, history, attachments=None):
        encoded = json.dumps(history, ensure_ascii=False)
        attachments = attachments or []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM outbox WHERE job_id=? LIMIT 1', (job_id,)).fetchone():
                return False
            seq = 0
            for body in chunks:
                db.execute('INSERT INTO outbox(job_id,seq,body) VALUES(?,?,?)', (job_id, seq, body))
                seq += 1
            # Anexos (PDF do gráfico) viram partes de mídia, enviadas após o texto.
            for item in attachments:
                db.execute('INSERT INTO outbox(job_id,seq,body,media_path,filename) VALUES(?,?,?,?,?)',
                           (job_id, seq, item.get('caption', ''), item['path'], item.get('filename', 'grafico.pdf')))
                seq += 1
            db.execute('INSERT OR REPLACE INTO conversations VALUES(?,?,?,?)',
                       (key, revision, encoded, time.time()))
            db.execute("UPDATE jobs SET status='ready' WHERE id=?", (job_id,))
        return True

    def pending_parts(self, job_id):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox WHERE job_id=? AND status='pending' ORDER BY seq", (job_id,))]

    def error_reply(self, job_id, text):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM outbox WHERE job_id=? LIMIT 1', (job_id,)).fetchone():
                return False
            db.execute('INSERT INTO outbox(job_id,seq,body) VALUES(?,0,?)', (job_id, text))
            db.execute("UPDATE jobs SET status='ready',error='A análise falhou; enviada uma orientação ao usuário.' WHERE id=?", (job_id,))
        return True

    def mark_part(self, job_id, seq, status, meta_id=None, error=None):
        with self.db() as db:
            db.execute('UPDATE outbox SET status=?,meta_id=?,error=? WHERE job_id=? AND seq=?',
                       (status, meta_id, error, job_id, seq))

    def mark_job(self, job_id, status, error=None):
        with self.db() as db:
            db.execute('UPDATE jobs SET status=?,error=? WHERE id=?', (status, error, job_id))

    def stats(self):
        with self.db() as db:
            return {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM jobs GROUP BY status')}

    def latest(self, limit=15):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT id,status,error,received FROM jobs ORDER BY received DESC LIMIT ?', (limit,))]
