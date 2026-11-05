"""Durable crossing records are independent of readable plates and image events."""
from datetime import datetime, timezone
import json
import uuid
from pathlib import Path

SCHEMA='''
CREATE TABLE IF NOT EXISTS live_detections(session_id TEXT NOT NULL,track_id INTEGER NOT NULL,occurred_at TEXT NOT NULL,source_seconds REAL NOT NULL,result TEXT NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(session_id,track_id));
CREATE TABLE IF NOT EXISTS camera_sessions(id TEXT PRIMARY KEY,camera TEXT NOT NULL,started_at TEXT NOT NULL,status TEXT NOT NULL,config TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS crossings(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES camera_sessions(id),camera TEXT NOT NULL,track_id INTEGER NOT NULL,sequence INTEGER NOT NULL,direction TEXT NOT NULL CHECK(direction IN ('entry','exit')),source_seconds REAL NOT NULL,occurred_at TEXT NOT NULL,plate TEXT NOT NULL DEFAULT '',original_plate TEXT NOT NULL DEFAULT '',confirmed INTEGER NOT NULL DEFAULT 0,authorization TEXT NOT NULL DEFAULT 'unknown',registry_snapshot TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',reason TEXT NOT NULL DEFAULT 'awaiting recognition',evidence TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,finalized INTEGER NOT NULL DEFAULT 0,UNIQUE(session_id,track_id,sequence));
CREATE TABLE IF NOT EXISTS crossing_reviews(id INTEGER PRIMARY KEY,crossing_id TEXT NOT NULL REFERENCES crossings(id),user_id INTEGER NOT NULL,created_at TEXT NOT NULL,old_value TEXT NOT NULL,new_value TEXT NOT NULL,note TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS crossing_camera_time ON crossings(camera,occurred_at);
'''

def utcnow(): return datetime.now(timezone.utc).isoformat()

def initialize(store):
    with store.connect() as c:
        c.executescript(SCHEMA)
        names={r[1] for r in c.execute('PRAGMA table_info(vehicles)')}
        for name in ('valid_from','valid_until'):
            if name not in names: c.execute(f'ALTER TABLE vehicles ADD COLUMN {name} TEXT')

    with store.connect() as c:
        names={r[1] for r in c.execute('PRAGMA table_info(events)')}
        for name,default in [('authorization',"'unknown'"),('registry_snapshot',"'{}'")]:
            if name not in names:c.execute(f'ALTER TABLE events ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}')

class LiveStore:
    def __init__(self, store):
        self.store=store
        initialize(store)

    def session(self, camera, config, started_at):
        sid=uuid.uuid4().hex
        with self.store.connect() as c:
            c.execute("UPDATE crossings SET finalized=1,reason='interrupted before finalization; review required',version=version+1 WHERE camera=? AND finalized=0",(camera,))
            c.execute("UPDATE camera_sessions SET status='interrupted' WHERE camera=? AND status='running'",(camera,))
            c.execute('INSERT INTO camera_sessions(id,camera,started_at,status,config) VALUES(?,?,?,?,?)',(sid,camera,started_at,'running',json.dumps(config)))
        return sid

    def finish(self,sid,status,summary):
        with self.store.connect() as c:
            c.execute('UPDATE camera_sessions SET status=?,summary=? WHERE id=?',(status,json.dumps(summary),sid))

    def stage(self,sid,camera,tid,crossing,occurred_at,evidence):
        eid=uuid.uuid4().hex
        with self.store.connect() as c:
            manual=c.execute('SELECT id FROM crossings WHERE session_id=? AND track_id=? AND sequence=-1 AND direction=?',(sid,tid,crossing['direction'])).fetchone()
            if manual and crossing['sequence']==1:return manual['id']
            c.execute('INSERT OR IGNORE INTO crossings(id,session_id,camera,track_id,sequence,direction,source_seconds,occurred_at,evidence) VALUES(?,?,?,?,?,?,?,?,?)',
                      (eid,sid,camera,tid,crossing['sequence'],crossing['direction'],crossing['source_seconds'],occurred_at,json.dumps(evidence)))
            return c.execute('SELECT id FROM crossings WHERE session_id=? AND track_id=? AND sequence=?',(sid,tid,crossing['sequence'])).fetchone()[0]

    @staticmethod
    def eligibility(c,plate,occurred_at,confirmed):
        row=c.execute('SELECT * FROM vehicles WHERE plate=?',(plate,)).fetchone()
        snapshot=dict(row) if row else {}
        if not confirmed: return 'unknown',snapshot
        when=datetime.fromisoformat(occurred_at)
        allowed=bool(row and row['active'] and (not row['valid_from'] or datetime.fromisoformat(row['valid_from'])<=when) and (not row['valid_until'] or when<=datetime.fromisoformat(row['valid_until'])))
        return ('allowed' if allowed else 'denied'),snapshot

    def finalize(self,eid,result,evidence,force_review=False):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM crossings WHERE id=?',(eid,)).fetchone()
            if row['finalized']: return
            confirmed=bool(result['confirmed'] and not force_review)
            authorization,snapshot=self.eligibility(c,result['plate'],row['occurred_at'],confirmed)
            duplicate=False
            if confirmed:
                prior=c.execute("SELECT * FROM crossings WHERE camera=? AND plate=? AND id<>? AND occurred_at<=? AND status!='dismissed' ORDER BY occurred_at DESC LIMIT 1",(row['camera'],result['plate'],eid,row['occurred_at'])).fetchone()
                duplicate=bool(prior and prior['direction']==row['direction'] and 0<=(datetime.fromisoformat(row['occurred_at'])-datetime.fromisoformat(prior['occurred_at'])).total_seconds()<8)
            if confirmed:
                images=c.execute('SELECT occurred_at FROM events WHERE plate=? AND direction=?',(result['plate'],row['direction'])).fetchall()
                duplicate=duplicate or any(abs((datetime.fromisoformat(row['occurred_at'])-datetime.fromisoformat(x[0])).total_seconds())<60 for x in images)
            status='accepted' if confirmed and not duplicate else 'pending'
            reason='possible duplicate' if duplicate else 'confirmed' if confirmed else 'recognition needs review'
            c.execute('UPDATE crossings SET plate=?,original_plate=?,confirmed=?,authorization=?,registry_snapshot=?,status=?,reason=?,evidence=?,finalized=1,version=version+1 WHERE id=?',
                      (result['plate'],result['plate'],int(confirmed),authorization,json.dumps(snapshot),status,reason,json.dumps(evidence),eid))

    def detection(self,sid,tid,when,t,result,evidence):
        with self.store.connect() as c:
            old=c.execute('SELECT evidence FROM live_detections WHERE session_id=? AND track_id=?',(sid,tid)).fetchone()
            if old and json.loads(old['evidence']).get('saved_event_id'):
                evidence={**evidence,'saved_event_id':json.loads(old['evidence'])['saved_event_id']}
            c.execute("INSERT INTO live_detections VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,track_id) DO UPDATE SET occurred_at=excluded.occurred_at,source_seconds=excluded.source_seconds,result=excluded.result,evidence=excluded.evidence",(sid,tid,when,t,json.dumps(result),json.dumps(evidence)))
            if old:
                name=json.loads(old['evidence']).get('image')
                if name and name!=evidence.get('image') and not c.execute("SELECT 1 FROM crossings WHERE json_extract(evidence,'$.image')=? LIMIT 1",(name,)).fetchone():
                    try:(self.store.path.parent/'media'/Path(name).name).unlink(missing_ok=True)
                    except PermissionError:pass  # Windows may still be serving this immutable evidence image.

    def record_detection(self,sid,tid,direction,plate=None,user_id=None):
        """Atomic, idempotent per-track record; direction is explicitly configured or declared."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT d.*,s.camera FROM live_detections d JOIN camera_sessions s ON s.id=d.session_id WHERE session_id=? AND track_id=?',(sid,tid)).fetchone()
            if not row:raise ValueError('Detection not found')
            prior=c.execute("SELECT id FROM crossings WHERE session_id=? AND track_id=? AND status!='dismissed' ORDER BY sequence DESC LIMIT 1",(sid,tid)).fetchone()
            if prior:return prior['id']
            result=json.loads(row['result']);evidence=json.loads(row['evidence'])
            if evidence.get('saved_event_id') and c.execute('SELECT 1 FROM crossings WHERE id=?',(evidence['saved_event_id'],)).fetchone():
                return evidence['saved_event_id']
            from .domain import normalize,valid_plate
            chosen=normalize(plate if plate is not None else result['plate'])
            if not valid_plate(chosen):raise ValueError('Verify a valid plate before recording')
            if user_id is None and not result['confirmed']:raise ValueError('Waiting for multi-frame confirmation')
            prior=c.execute("SELECT id FROM crossings WHERE camera=? AND plate=? AND direction=? AND status!='dismissed' AND ABS((julianday(occurred_at)-julianday(?))*86400)<8 LIMIT 1",(row['camera'],chosen,direction,row['occurred_at'])).fetchone()
            if prior:
                evidence['saved_event_id']=prior['id']
                c.execute('UPDATE live_detections SET evidence=? WHERE session_id=? AND track_id=?',(json.dumps(evidence),sid,tid))
                return prior['id']
            prior=c.execute("SELECT id FROM events WHERE plate=? AND direction=? AND ABS((julianday(occurred_at)-julianday(?))*86400)<60 LIMIT 1",(chosen,direction,row['occurred_at'])).fetchone()
            if prior:raise ValueError('A matching image event already exists within 60 seconds')
            authorization,snapshot=self.eligibility(c,chosen,row['occurred_at'],True)
            evidence['event_basis']='operator_declared' if user_id is not None else 'configured_lane'
            eid=uuid.uuid4().hex
            c.execute("INSERT INTO crossings(id,session_id,camera,track_id,sequence,direction,source_seconds,occurred_at,plate,original_plate,confirmed,authorization,registry_snapshot,status,reason,evidence,finalized) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (eid,sid,row['camera'],tid,-1,direction,row['source_seconds'],row['occurred_at'],chosen,result['plate'],int(result['confirmed'] and chosen==result['plate']),authorization,json.dumps(snapshot),'accepted','operator declared' if user_id is not None else 'confirmed; configured '+direction+' lane',json.dumps(evidence)))
            if user_id is not None:
                c.execute('INSERT INTO crossing_reviews(crossing_id,user_id,created_at,old_value,new_value,note) VALUES(?,?,?,?,?,?)',(eid,user_id,utcnow(),'{}',json.dumps({'plate':chosen,'direction':direction}),'Operator verified live detection and declared passage'))
            return eid
