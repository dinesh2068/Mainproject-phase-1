from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from queue import Queue, Full
from threading import Thread, Lock
from typing import Literal
import csv
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import sqlite3
import time
import uuid
import warnings

import numpy as np
from PIL import Image, ImageOps, ImageDraw, UnidentifiedImageError
from fastapi import FastAPI, Depends, HTTPException, Request, Response, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .storage import Store
from .security import password_hash, verify_password, token_hash
from .domain import normalize, valid_plate
from .vision import PlateEngine
from .inputs import ImageInput
from .pipeline import recognize_frame

ROOT = Path(__file__).resolve().parents[2]

def now():
    return datetime.now(timezone.utc).isoformat()


def decode_image(raw, max_pixels):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in ('JPEG', 'PNG') or im.width*im.height > max_pixels:
                    raise ValueError('Use JPEG/PNG, up to 20 megapixels.')
                return ImageOps.exif_transpose(im).convert('RGB')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('The file is not a valid supported image.') from exc


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    password: str = Field(min_length=1, max_length=128)

class NewUser(Credentials):
    role: Literal['admin','operator'] = 'operator'

class Review(BaseModel):
    plate: str = Field(max_length=20)
    decision: Literal['accepted','rejected']
    note: str = Field(min_length=3, max_length=500)
    version: int

class Vehicle(BaseModel):
    plate: str = Field(min_length=1, max_length=20)
    owner: str = Field(min_length=1, max_length=100)
    category: Literal['Staff','Student','Visitor','Service'] = 'Staff'
    active: bool = True

class Event(BaseModel):
    direction: Literal['entry','exit']
    occurred_at: datetime


def create_app(data_dir=None, engine=None):
    data = Path(data_dir or os.getenv('AVACLS_DATA_DIR', ROOT / 'data')).resolve()
    data.mkdir(parents=True, exist_ok=True)
    media = data / 'media'; media.mkdir(exist_ok=True)
    cfg = json.loads((ROOT / 'config.json').read_text())
    bounds = {'plate_confidence':(.01,1),'ocr_confidence':(.01,1),'image_size':(320,1920),
              'max_upload_mb':(1,50),'max_pixels':(1000,40_000_000),'queue_size':(1,32),
              'retention_days':(1,3650),'session_hours':(1,24)}
    for key,(low,high) in bounds.items():
        if not isinstance(cfg.get(key),(int,float)) or isinstance(cfg[key],bool) or not low<=cfg[key]<=high:
            raise ValueError(f'config.json: {key} must be between {low} and {high}.')
    for key in ('image_size','max_upload_mb','max_pixels','queue_size','retention_days','session_hours'):
        if not isinstance(cfg[key],int): raise ValueError(f'config.json: {key} must be an integer.')
    store = Store(data / 'avacls.sqlite3')
    queue = Queue(maxsize=cfg['queue_size'])
    submission_lock = Lock()
    stop = object()
    logger = logging.getLogger(f'avacls.{data.name}.{id(store)}')
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(data/'application.log', maxBytes=2_000_000, backupCount=3)
    logger.addHandler(handler)
    engine = engine or PlateEngine(ROOT/'models', cfg)
    state = {'model':'not_loaded', 'last_error':None}
    attempts = {}
    auth_lock = Lock()

    def audit(c, user_id, action, detail):
        c.execute('INSERT INTO audit(user_id,created_at,action,detail) VALUES(?,?,?,?)',
                  (user_id, now(), action, detail))

    def cleanup():
        cutoff = (datetime.now(timezone.utc)-timedelta(days=cfg['retention_days'])).isoformat()
        with store.connect() as c:
            rows = c.execute("SELECT id,image_path,annotated_path FROM jobs WHERE created_at<? AND status NOT IN ('queued','processing')", (cutoff,)).fetchall()
            for r in rows:
                paths = [r['image_path'], r['annotated_path']]
                paths += [v[0] for v in c.execute('SELECT crop_path FROM observations WHERE job_id=?',(r['id'],))]
                for p in paths:
                    if p: (media/p).unlink(missing_ok=True)
                c.execute("UPDATE jobs SET image_path='',annotated_path=NULL WHERE id=?",(r['id'],))
                c.execute('UPDATE observations SET crop_path=NULL WHERE job_id=?',(r['id'],))
            c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))

    def process(job_id):
        with store.connect() as c:
            job = c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            c.execute("UPDATE jobs SET status='processing' WHERE id=?",(job_id,))
        start = time.perf_counter()
        try:
            state['model'] = 'loading' if state['model'] != 'ready' else 'ready'
            frame = ImageInput(media/job['image_path']).read()
            observations, annotated_path = recognize_frame(frame, engine, media, job_id, cfg['ocr_confidence'])
            state.update(model='ready', last_error=None)
            rows = [(p.id,job_id,p.raw_text,p.plate,p.plate,p.detection_confidence,p.ocr_confidence,
                     json.dumps(p.box),json.dumps(p.warnings),int(p.format_valid),'pending',p.crop_path)
                    for p in observations]
            with store.connect() as c:
                c.executemany('INSERT INTO observations(id,job_id,raw_text,original_plate,plate,detection_confidence,ocr_confidence,box,warnings,format_valid,status,crop_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',rows)
                c.execute("UPDATE jobs SET status='completed',annotated_path=?,processing_seconds=? WHERE id=?",(annotated_path,round(time.perf_counter()-start,3),job_id))
            logger.info('Completed job %s with %s observations',job_id,len(rows))
        except Exception:
            logger.exception('Recognition failed for job %s',job_id)
            message = 'Recognition failed. Check Settings and application.log; verify AI dependencies and bundled models.'
            state.update(model='error',last_error=message)
            with store.connect() as c:
                c.execute("UPDATE jobs SET status='failed',error=? WHERE id=?",(message,job_id))
        try:
            cleanup()
        except Exception:
            logger.exception('Media retention cleanup failed')

    def worker():
        while True:
            item = queue.get()
            try:
                if item is stop: return
                process(item)
            except Exception:
                logger.exception('Worker recovered from job handling failure')
            finally:
                queue.task_done()

    @asynccontextmanager
    async def lifespan(app):
        # Interrupted jobs are explicit failures; users can retry without invented results.
        with store.connect() as c:
            c.execute("UPDATE jobs SET status='failed',error='Processing interrupted by restart. Upload the image again.' WHERE status IN ('queued','processing')")
        cleanup()
        thread = Thread(target=worker,daemon=True,name='recognition'); thread.start()
        yield
        if hasattr(app.state,'camera'): app.state.camera.close()
        queue.put(stop); thread.join(timeout=30)
        handler.close(); logger.removeHandler(handler)

    app = FastAPI(title='AVACLS local pilot',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None)
    app.state.store = store

    @app.middleware('http')
    async def guard(request: Request, call_next):
        if request.method in ('POST','PUT','PATCH','DELETE'):
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                # Vite development origin only; production is same-origin.
                if not (os.getenv('AVACLS_DEV')=='1' and origin=='http://127.0.0.1:5173'):
                    return Response('Cross-origin writes are not allowed',status_code=403)
            length = request.headers.get('content-length')
            if length and (not length.isdigit() or int(length)>cfg['max_upload_mb']*1024*1024):
                return Response('Upload exceeds configured limit',status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith('/api'): response.headers['Cache-Control']='no-store'
        return response

    def user(request: Request):
        token = request.cookies.get('avacls_session','')
        with store.connect() as c:
            row = c.execute('SELECT u.id,u.username,u.role,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?',(token_hash(token),time.time())).fetchone()
        if not row: raise HTTPException(401,'Please sign in.')
        if request.method not in ('GET','HEAD') and not secrets.compare_digest(request.headers.get('x-csrf-token',''),row['csrf']):
            raise HTTPException(403,'Session security token missing. Refresh and try again.')
        return dict(row)

    def admin(u=Depends(user)):
        if u['role']!='admin': raise HTTPException(403,'Administrator access required.')
        return u

    @app.get('/api/bootstrap')
    def bootstrap():
        with store.connect() as c:
            needed = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0
        return {'setup_required':needed}

    @app.post('/api/setup',status_code=201)
    def setup(body: Credentials):
        if len(body.password)<12: raise HTTPException(422,'Use at least 12 characters for the password.')
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT COUNT(*) FROM users').fetchone()[0]: raise HTTPException(409,'Setup is already complete.')
            c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)',(body.username,password_hash(body.password),'admin'))
        return {'ok':True}

    @app.post('/api/login')
    def login(body: Credentials, request: Request, response: Response):
        ip = request.client.host if request.client else 'local'
        with auth_lock:
            recent = [t for t in attempts.get(ip,[]) if t>time.time()-60]
            if len(recent)>=10: raise HTTPException(429,'Too many attempts. Wait one minute.')
            recent.append(time.time()); attempts[ip]=recent
        with store.connect() as c:
            row = c.execute('SELECT * FROM users WHERE username=?',(body.username,)).fetchone()
            if not row or not verify_password(body.password,row['password']): raise HTTPException(401,'Incorrect username or password.')
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(token_hash(token),row['id'],time.time()+cfg['session_hours']*3600,csrf))
            audit(c,row['id'],'login',row['username'])
        response.set_cookie('avacls_session',token,httponly=True,samesite='strict',secure=os.getenv('AVACLS_HTTPS')=='1',max_age=cfg['session_hours']*3600)
        return {'id':row['id'],'username':row['username'],'role':row['role'],'csrf':csrf}

    @app.get('/api/me')
    def me(u=Depends(user)): return u

    @app.post('/api/logout')
    def logout(request:Request,response:Response,u=Depends(user)):
        with store.connect() as c: c.execute('DELETE FROM sessions WHERE token=?',(token_hash(request.cookies.get('avacls_session','')),))
        response.delete_cookie('avacls_session'); return {'ok':True}

    @app.post('/api/users',status_code=201)
    def add_user(body:NewUser,u=Depends(admin)):
        if len(body.password)<12: raise HTTPException(422,'Use at least 12 characters.')
        try:
            with store.connect() as c:
                c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)',(body.username,password_hash(body.password),body.role))
                audit(c,u['id'],'create_user',body.username)
        except sqlite3.IntegrityError: raise HTTPException(409,'Username already exists.')
        return {'ok':True}

    @app.get('/api/health')
    def health(u=Depends(user)):
        return {'database':'ready','model':state['model'],'last_error':state['last_error'],'queue_depth':queue.qsize(),'mode':'live_and_image','config':cfg}

    @app.get('/api/stats')
    def stats(u=Depends(user)):
        with store.connect() as c:
            return {'images':c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],
                    'pending':c.execute("SELECT COUNT(*) FROM observations WHERE status='pending'").fetchone()[0],
                    'vehicles':c.execute('SELECT COUNT(*) FROM vehicles WHERE active=1').fetchone()[0],
                    'events':c.execute('SELECT COUNT(*) FROM events').fetchone()[0]}

    @app.post('/api/jobs',status_code=202)
    async def upload(request:Request,u=Depends(user)):
        # Raw body is streamed with a hard cap before decoding; no unbounded multipart spool.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>cfg['max_upload_mb']*1024*1024: raise HTTPException(413,'Maximum upload is 10 MB.')
        try: im = decode_image(raw,cfg['max_pixels'])
        except ValueError as e: raise HTTPException(422,str(e))
        jid = uuid.uuid4().hex
        filename = request.headers.get('x-filename','image')[:200]
        with submission_lock:
            if queue.full(): raise HTTPException(429,'Processing queue is full. Try again shortly.')
            im.save(media/(jid+'.jpg'),quality=95)
            with store.connect() as c:
                c.execute('INSERT INTO jobs(id,created_at,user_id,filename,status,image_path,width,height) VALUES(?,?,?,?,?,?,?,?)',
                          (jid,now(),u['id'],filename,'queued',jid+'.jpg',im.width,im.height))
            queue.put_nowait(jid)
        return {'id':jid,'status':'queued'}

    @app.get('/api/jobs')
    def jobs(page:int=Query(1,ge=1),q:str=Query('',max_length=100),u=Depends(user)):
        where="WHERE filename LIKE ? OR id IN (SELECT job_id FROM observations WHERE plate LIKE ?)"
        with store.connect() as c:
            params=('%'+q+'%','%'+q+'%')
            count=c.execute('SELECT COUNT(*) FROM jobs '+where,params).fetchone()[0]
            rows=c.execute('SELECT * FROM jobs '+where+' ORDER BY created_at DESC LIMIT 12 OFFSET ?',(*params,(page-1)*12)).fetchall()
        return {'items':[dict(r) for r in rows],'total':count,'page':page}

    @app.get('/api/jobs/{jid}')
    def job(jid:str,u=Depends(user)):
        with store.connect() as c:
            row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row: raise HTTPException(404,'Image not found.')
            result=dict(row)
            observations=c.execute('SELECT o.*,v.owner,v.active AS registered_active,e.id AS event_id FROM observations o LEFT JOIN vehicles v ON v.plate=o.plate LEFT JOIN events e ON e.observation_id=o.id WHERE job_id=?',(jid,)).fetchall()
            result['observations']=[]
            for r in observations:
                item=dict(r); item['warnings']=json.loads(item['warnings']); item['box']=json.loads(item['box'])
                item['reviews']=[dict(a) for a in c.execute('SELECT r.*,u.username FROM reviews r JOIN users u ON r.user_id=u.id WHERE observation_id=? ORDER BY r.id',(r['id'],))]
                from .live_store import LiveStore
                eligibility,_=LiveStore.eligibility(c,item['plate'],now(),bool(item['format_valid']))
                item['registered_active']=eligibility=='allowed'
                result['observations'].append(item)
        return result

    @app.get('/api/reviews')
    def pending(page:int=Query(1,ge=1),u=Depends(user)):
        with store.connect() as c:
            rows=c.execute("SELECT o.*,j.filename,j.created_at FROM observations o JOIN jobs j ON j.id=o.job_id WHERE o.status='pending' ORDER BY j.created_at LIMIT 12 OFFSET ?",((page-1)*12,)).fetchall()
            total=c.execute("SELECT COUNT(*) FROM observations WHERE status='pending'").fetchone()[0]
        return {'items':[dict(r) for r in rows],'total':total}

    @app.post('/api/observations/{oid}/review')
    def review(oid:str,body:Review,u=Depends(user)):
        plate=normalize(body.plate)
        if len(body.note.strip())<3: raise HTTPException(422,'Provide a meaningful review note.')
        if body.decision=='accepted' and not valid_plate(plate): raise HTTPException(422,'Acceptance requires a supported plate format. Reject unsupported plates with a note.')
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM observations WHERE id=?',(oid,)).fetchone()
            if not row: raise HTTPException(404,'Observation not found.')
            if row['version']!=body.version: raise HTTPException(409,'Another operator changed this result. Reload it.')
            if c.execute('SELECT 1 FROM events WHERE observation_id=?',(oid,)).fetchone(): raise HTTPException(409,'This observation has a gate event and is locked for audit.')
            c.execute('UPDATE observations SET plate=?,status=?,format_valid=?,version=version+1 WHERE id=?',(plate,body.decision,int(valid_plate(plate)),oid))
            c.execute('INSERT INTO reviews(observation_id,user_id,created_at,old_plate,new_plate,decision,note) VALUES(?,?,?,?,?,?,?)',(oid,u['id'],now(),row['plate'],plate,body.decision,body.note.strip()))
            audit(c,u['id'],'review',oid)
        return {'ok':True}

    @app.post('/api/observations/{oid}/event',status_code=201)
    def event(oid:str,body:Event,u=Depends(user)):
        if body.occurred_at.tzinfo is None: raise HTTPException(422,'Timestamp must include a timezone.')
        if body.occurred_at>datetime.now(timezone.utc)+timedelta(minutes=5): raise HTTPException(422,'Event time cannot be in the future.')
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM observations WHERE id=?',(oid,)).fetchone()
            if not row or row['status']!='accepted': raise HTTPException(409,'Accept the recognition before recording an event.')
            if c.execute('SELECT 1 FROM events WHERE observation_id=?',(oid,)).fetchone(): raise HTTPException(409,'An event already exists for this observation.')
            # Do not mistake repeated uploads for new passages within one minute.
            recent=c.execute("SELECT occurred_at FROM events WHERE plate=? AND direction=? UNION ALL SELECT occurred_at FROM crossings WHERE plate=? AND direction=? AND status!='dismissed'",(row['plate'],body.direction,row['plate'],body.direction)).fetchall()
            if any(abs((body.occurred_at-datetime.fromisoformat(r[0])).total_seconds())<60 for r in recent): raise HTTPException(409,'Possible duplicate: same plate and direction within 60 seconds.')
            eid=uuid.uuid4().hex
            c.execute('INSERT INTO events(id,observation_id,plate,direction,occurred_at,recorded_at,user_id) VALUES(?,?,?,?,?,?,?)',(eid,oid,row['plate'],body.direction,body.occurred_at.astimezone(timezone.utc).isoformat(),now(),u['id']))
            from .live_store import LiveStore
            authorization,snapshot=LiveStore.eligibility(c,row['plate'],body.occurred_at.isoformat(),True)
            c.execute('UPDATE events SET authorization=?,registry_snapshot=? WHERE id=?',(authorization,json.dumps(snapshot),eid))
            audit(c,u['id'],'record_event',eid)
        return {'id':eid}

    @app.get('/api/vehicles')
    def vehicles(q:str=Query('',max_length=100),page:int=Query(1,ge=1),u=Depends(user)):
        with store.connect() as c:
            args=('%'+q+'%','%'+q+'%')
            total=c.execute('SELECT COUNT(*) FROM vehicles WHERE plate LIKE ? OR owner LIKE ?',args).fetchone()[0]
            rows=c.execute('SELECT * FROM vehicles WHERE plate LIKE ? OR owner LIKE ? ORDER BY plate LIMIT 12 OFFSET ?',(*args,(page-1)*12)).fetchall()
        return {'items':[dict(r) for r in rows],'total':total}

    @app.put('/api/vehicles')
    def save_vehicle(body:Vehicle,u=Depends(admin)):
        plate=normalize(body.plate)
        if not valid_plate(plate) or not body.owner.strip(): raise HTTPException(422,'Enter a supported plate and owner name.')
        with store.connect() as c:
            c.execute('INSERT INTO vehicles(plate,owner,category,active,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(plate) DO UPDATE SET owner=excluded.owner,category=excluded.category,active=excluded.active,updated_at=excluded.updated_at',(plate,body.owner.strip(),body.category,int(body.active),now()))
            audit(c,u['id'],'save_vehicle',plate)
        return {'ok':True}

    def event_rows(q,direction,page=None):
        sql='SELECT e.*,u.username FROM events e JOIN users u ON e.user_id=u.id WHERE e.plate LIKE ? AND (?=\'\' OR e.direction=?) ORDER BY e.occurred_at DESC'
        args=['%'+q+'%',direction,direction]
        if page is not None: sql+=' LIMIT 12 OFFSET ?'; args.append((page-1)*12)
        with store.connect() as c: return [dict(r) for r in c.execute(sql,args)]

    @app.get('/api/events')
    def events(q:str=Query('',max_length=100),direction:Literal['','entry','exit']='',page:int=Query(1,ge=1),u=Depends(user)):
        with store.connect() as c:
            total=c.execute("SELECT COUNT(*) FROM events WHERE plate LIKE ? AND (?='' OR direction=?)",('%'+q+'%',direction,direction)).fetchone()[0]
        return {'items':event_rows(q,direction,page),'total':total}

    @app.get('/api/events.csv')
    def export(q:str=Query('',max_length=100),direction:Literal['','entry','exit']='',u=Depends(user)):
        def generate():
            out=io.StringIO(); writer=csv.writer(out)
            fields=['plate','direction','occurred_at','recorded_at','username','basis']
            writer.writerow(fields); yield out.getvalue(); out.seek(0); out.truncate(0)
            for row in event_rows(q,direction):
                writer.writerow([("'"+str(row[k])) if str(row[k]).startswith(('=','+','-','@')) else row[k] for k in fields])
                yield out.getvalue(); out.seek(0); out.truncate(0)
        return StreamingResponse(generate(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="avacls-events.csv"'})

    @app.get('/api/media/{name}')
    def get_media(name:str,u=Depends(user)):
        import re
        if not re.fullmatch(r'[a-f0-9]{32}(?:-annotated)?\.jpg',name): raise HTTPException(404)
        path=media/name
        if not path.is_file(): raise HTTPException(404,'Image expired or unavailable.')
        return FileResponse(path,media_type='image/jpeg')

    from .live_api import attach
    attach(app,store,data,user,admin,ROOT)
    from .workspace_api import attach_workspace
    attach_workspace(app,store,data,user,admin,ROOT)

    dist=ROOT/'frontend'/'dist'
    if dist.exists(): app.mount('/',StaticFiles(directory=dist,html=True),name='web')
    return app
