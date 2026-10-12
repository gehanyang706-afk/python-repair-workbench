"""SQLite 任务队列与有序事件；每次操作使用独立连接和短事务。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

TERMINAL = {'completed', 'cancelled', 'interrupted', 'error'}


def now():
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL,
                    state TEXT NOT NULL, phase TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    result TEXT, error_type TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events ON events(task_id, sequence);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        value['request'] = json.loads(value['request'])
        value['result'] = json.loads(value['result']) if value['result'] else None
        return value

    @staticmethod
    def emit(db, task_id, payload):
        db.execute('INSERT INTO events(task_id, created_at, payload) VALUES(?,?,?)',
                   (task_id, now(), json.dumps(payload, ensure_ascii=False)))

    def create(self, request):
        task_id, timestamp = uuid4().hex, now()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued','running','cancelling')").fetchone()[0]
            if count >= 20:
                raise ValueError('队列已满，请等待现有任务完成')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)',
                       (task_id, json.dumps(request, ensure_ascii=False), 'queued', 'queued',
                        timestamp, timestamp, None, None))
            self.emit(db, task_id, {'event': 'queued'})
        return self.get(task_id)

    def get(self, task_id):
        with self.connection() as db:
            return self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def list(self):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100')]

    def claim(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET state='running',phase='snapshot',updated_at=? WHERE id=?", (now(), row['id']))
            self.emit(db, row['id'], {'event': 'started'})
            task_id = row['id']
        return self.get(task_id)

    def event(self, task_id, payload, phase=None, result=None):
        with self.connection() as db:
            self.emit(db, task_id, payload)
            if phase:
                db.execute('UPDATE tasks SET phase=?,updated_at=? WHERE id=?', (phase, now(), task_id))
            if result is not None:
                db.execute('UPDATE tasks SET result=?,updated_at=? WHERE id=?',
                           (json.dumps(result, ensure_ascii=False), now(), task_id))

    def cancel(self, task_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row['state'] in TERMINAL or row['state'] == 'cancelling':
                return
            state = 'cancelled' if row['state'] == 'queued' else 'cancelling'
            db.execute('UPDATE tasks SET state=?,updated_at=? WHERE id=?', (state, now(), task_id))
            self.emit(db, task_id, {'event': state})

    def finish(self, task_id, result, error_type=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            state = 'cancelled' if row['state'] == 'cancelling' else 'error' if error_type else 'completed'
            db.execute('UPDATE tasks SET state=?,phase=?,result=?,error_type=?,updated_at=? WHERE id=?',
                       (state, 'finished', json.dumps(result, ensure_ascii=False), error_type, now(), task_id))
            self.emit(db, task_id, {'event': 'finished', 'state': state})

    def recover(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id FROM tasks WHERE state IN ('running','cancelling')").fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET state='interrupted',phase='finished',updated_at=? WHERE id=?", (now(), row['id']))
                self.emit(db, row['id'], {'event': 'interrupted'})

    def events(self, task_id, after=0, limit=100):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM events WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                              (task_id, after, limit)).fetchall()
            return [{'id': row['sequence'], 'created_at': row['created_at'], **json.loads(row['payload'])} for row in rows]
