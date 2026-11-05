"""Process-safe preview snapshots: no shared JPEG file handles on Windows."""
import json,sqlite3,uuid,time
from pathlib import Path
from contextlib import contextmanager

class CameraRuntime:
    def __init__(self,data):
        self.path=Path(data)/'camera-runtime.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS runtime(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL, metadata TEXT NOT NULL, jpeg BLOB)')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()
    def begin(self,token=None):
        token=token or uuid.uuid4().hex
        meta={'state':'loading_models','frames':0,'source_seconds':0,'has_preview':False,'session_id':None,'updated_at':time.time()}
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO runtime VALUES(1,?,?,NULL)',(token,json.dumps(meta)))
        return token
    def update(self,token,jpeg=None,**fields):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT metadata FROM runtime WHERE id=1 AND token=?',(token,)).fetchone()
            if not row:return False
            meta=json.loads(row[0]);meta.update(fields,updated_at=time.time())
            if jpeg is not None:
                meta['has_preview']=True
                c.execute('UPDATE runtime SET metadata=?,jpeg=? WHERE id=1',(json.dumps(meta),jpeg))
            else:c.execute('UPDATE runtime SET metadata=? WHERE id=1',(json.dumps(meta),))
        return True
    def status(self):
        with self.connect() as c:row=c.execute('SELECT token,metadata FROM runtime WHERE id=1').fetchone()
        return {**json.loads(row[1]),'run_id':row[0]} if row else {'state':'stopped','frames':0,'has_preview':False}
    def preview(self):
        # Close the connection before handing owned bytes to the HTTP response.
        with self.connect() as c:row=c.execute('SELECT jpeg FROM runtime WHERE id=1').fetchone()
        return bytes(row[0]) if row and row[0] is not None else None
