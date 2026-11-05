"""Local camera lifecycle and unified records; no shell execution."""
import json, subprocess, sys, threading
from .camera_runtime import CameraRuntime
from pathlib import Path
from typing import Literal
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from fastapi.responses import RedirectResponse

class StartCamera(BaseModel):
    source: str = Field(min_length=1,max_length=1000)
    live: bool = True
    event_mode: Literal["crossing","entry","exit"] = "crossing"
    max_frames: int = Field(default=0,ge=0,le=1000000)

class CameraManager:
    def __init__(self,root,data):
        self.root,self.data=root,data
        self.runtime=CameraRuntime(data)
        self.process=None;self.log=None;self.lock=threading.Lock()
    def status(self):
        code=self.process.poll() if self.process else None
        progress=self.runtime.status()
        running=self.process is not None and code is None
        if code:progress['state']='failed'
        elif not running and progress['state'] in ('loading_models','opening_source','processing','stopping'):progress['state']='stopped'
        return {**progress,'running':running,'exit_code':code}
    def start(self,body):
        with self.lock:
            if self.status()['running']:raise HTTPException(409,'Camera is already running')
            source=body.source.strip()
            if source.isdigit() and not body.live:raise HTTPException(422,'USB input requires live mode')
            if '://' in source and not source.lower().startswith(('rtsp://','rtsps://','http://','https://')):raise HTTPException(422,'Unsupported camera protocol')
            if not source.isdigit() and '://' not in source:
                path=Path(source)
                if not path.is_absolute():path=self.root/path
                if not path.is_file():raise HTTPException(422,'Video file does not exist on the server')
                source=str(path.resolve())
            (self.data/'camera.stop').unlink(missing_ok=True)
            if self.log:self.log.close()
            self.log=(self.data/'camera-worker.log').open('ab')
            token=self.runtime.begin()
            args=[sys.executable,str(self.root/'tools/run_camera.py'),'--source',source,'--data',str(self.data),'--max-frames',str(body.max_frames),'--event-mode',body.event_mode,'--run-id',token]
            if body.live:args.append('--live')
            try:self.process=subprocess.Popen(args,cwd=self.root,stdout=self.log,stderr=subprocess.STDOUT)
            except Exception:
                self.runtime.update(token,state='failed',error='Could not start camera worker');raise
            return self.status()
    def stop(self):
        with self.lock:
            (self.data/'camera.stop').touch()
            if self.status()['running']:
                self.runtime.update(self.runtime.status()['run_id'],state='stopping')
            return {**self.status(),'stop_requested':True}
    def close(self):
        self.stop()
        if self.process and self.process.poll() is None:
            try:self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        if self.log:self.log.close()

class DetectionEvent(BaseModel):
    direction: Literal['entry','exit']
    plate: str = Field(min_length=1,max_length=20)

def attach_workspace(app,store,data,user,admin,root):
    from .live_store import LiveStore
    repo=LiveStore(store)
    @app.get('/api/camera/detections')
    def detections(u=Depends(user)):
        with store.connect() as c:
            runtime=CameraRuntime(data).status()
            if runtime.get('run_id'):
                session=c.execute('SELECT * FROM camera_sessions WHERE id=?',(runtime.get('session_id'),)).fetchone()
            else:
                session=c.execute('SELECT * FROM camera_sessions ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()
            if not session:return {'session':None,'items':[]}
            items=[]
            for row in c.execute('SELECT * FROM live_detections WHERE session_id=? ORDER BY source_seconds DESC LIMIT 50',(session['id'],)):
                item=dict(row);item['result']=json.loads(item['result']);item['evidence']=json.loads(item['evidence'])
                item['authorization'],item['registry']=repo.eligibility(c,item['result']['plate'],item['occurred_at'],item['result']['confirmed'])
                event=c.execute("SELECT id,direction,status,reason,plate FROM crossings WHERE session_id=? AND track_id=? ORDER BY source_seconds DESC LIMIT 1",(session['id'],item['track_id'])).fetchone()
                if not event and item['evidence'].get('saved_event_id'):
                    event=c.execute('SELECT id,direction,status,reason,plate FROM crossings WHERE id=?',(item['evidence']['saved_event_id'],)).fetchone()
                item['event']=dict(event) if event else None;items.append(item)
            return {'session':dict(session),'items':items}
    @app.post('/api/camera/detections/{sid}/{tid}/event')
    def detection_event(sid:str,tid:int,body:DetectionEvent,u=Depends(user)):
        try:eid=repo.record_detection(sid,tid,body.direction,body.plate,u['id'])
        except ValueError as e:raise HTTPException(409,str(e))
        return {'id':eid}

    manager=CameraManager(root,data);app.state.camera=manager
    @app.get('/api/camera/status')
    def status(u=Depends(user)):return manager.status()
    @app.post('/api/camera/start')
    def start(body:StartCamera,u=Depends(admin)):return manager.start(body)
    @app.post('/api/camera/stop')
    def stop(u=Depends(admin)):return manager.stop()
    @app.get('/api/records')
    def records(q:str=Query('',max_length=100),direction:Literal['','entry','exit']='',page:int=Query(1,ge=1),u=Depends(user)):
        sql="""SELECT id,plate,direction,occurred_at,'image' AS source,'accepted' AS status,authorization,registry_snapshot,'operator declared' AS reason,1 AS finalized,1 AS version FROM events
        UNION ALL SELECT id,plate,direction,occurred_at,'video',status,authorization,registry_snapshot,reason,finalized,version FROM crossings"""
        args=('%'+q+'%',direction,direction)
        where=" WHERE plate LIKE ? AND (?='' OR direction=?)"
        with store.connect() as c:
            total=c.execute('SELECT COUNT(*) FROM ('+sql+')'+where,args).fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT * FROM ('+sql+')'+where+' ORDER BY occurred_at DESC LIMIT 12 OFFSET ?',(*args,(page-1)*12))]
        return {'items':rows,'total':total}
