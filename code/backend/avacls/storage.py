import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','operator')));
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires REAL NOT NULL, csrf TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, user_id INTEGER REFERENCES users(id), filename TEXT NOT NULL, status TEXT NOT NULL, image_path TEXT NOT NULL, annotated_path TEXT, width INTEGER, height INTEGER, error TEXT, processing_seconds REAL, source TEXT NOT NULL DEFAULT 'image');
CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), raw_text TEXT NOT NULL, original_plate TEXT NOT NULL, plate TEXT NOT NULL, detection_confidence REAL NOT NULL, ocr_confidence REAL NOT NULL, box TEXT NOT NULL, warnings TEXT NOT NULL, format_valid INTEGER NOT NULL, status TEXT NOT NULL, crop_path TEXT, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS vehicles(plate TEXT PRIMARY KEY, owner TEXT NOT NULL, category TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, observation_id TEXT REFERENCES observations(id), user_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL, old_plate TEXT NOT NULL, new_plate TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, observation_id TEXT UNIQUE REFERENCES observations(id), plate TEXT NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('entry','exit')), occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, user_id INTEGER REFERENCES users(id), basis TEXT NOT NULL DEFAULT 'operator_declared');
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, user_id INTEGER, created_at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS ix_observation_job ON observations(job_id);
CREATE INDEX IF NOT EXISTS ix_observation_status ON observations(status);
PRAGMA user_version=1;
'''

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            if c.execute('PRAGMA user_version').fetchone()[0]>1:
                raise RuntimeError('Database schema is newer than this application. Use the matching app version.')
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA journal_mode=WAL')
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
