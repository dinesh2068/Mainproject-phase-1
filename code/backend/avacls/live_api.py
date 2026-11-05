import csv,io,json
from datetime import datetime,timezone
from typing import Literal
from fastapi import Depends,HTTPException,Query
from fastapi.responses import FileResponse,StreamingResponse,Response
from .camera_runtime import CameraRuntime
from pydantic import BaseModel,Field
from .domain import normalize
from .live_store import LiveStore,utcnow
class ReviewCrossing(BaseModel):
    plate:str=Field(default='',max_length=20,pattern=r'^[a-zA-Z0-9 ]*$')
    decision:Literal['accepted','dismissed']
    note:str=Field(min_length=3,max_length=500)
    version:int
class Validity(BaseModel):
    valid_from:datetime|None=None
    valid_until:datetime|None=None

def attach(app,store,data,user,admin,root):
    repo=LiveStore(store)
    @app.get('/camera')
    def page():return FileResponse(root/'frontend/dist/index.html')
    @app.get('/api/camera/preview')
    def preview(u=Depends(user)):
        jpeg=CameraRuntime(data).preview()
        if jpeg is None:raise HTTPException(404,'Waiting for the first processed frame')
        return Response(content=jpeg,media_type='image/jpeg',headers={'Cache-Control':'no-store'})
    @app.get('/api/camera/sessions')
    def sessions(u=Depends(user)):
        with store.connect() as c:return {'items':[dict(r) for r in c.execute('SELECT * FROM camera_sessions ORDER BY started_at DESC LIMIT 30')]}
    @app.get('/api/crossings')
    def crossings(status:Literal['','pending','accepted','dismissed']='',page:int=Query(1,ge=1),u=Depends(user)):
        with store.connect() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM crossings WHERE (?='' OR status=?) ORDER BY occurred_at DESC LIMIT 30 OFFSET ?",(status,status,(page-1)*30))]
            counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM crossings GROUP BY status')}
        return {'items':rows,'counts':counts}
    @app.get('/api/crossings/{eid}')
    def detail(eid:str,u=Depends(user)):
        with store.connect() as c:
            row=c.execute('SELECT * FROM crossings WHERE id=?',(eid,)).fetchone()
            if not row:raise HTTPException(404)
            result=dict(row);result['evidence']=json.loads(result['evidence'])
            result['reviews']=[dict(r) for r in c.execute('SELECT * FROM crossing_reviews WHERE crossing_id=? ORDER BY id',(eid,))]
            return result
    @app.post('/api/crossings/{eid}/review')
    def review(eid:str,body:ReviewCrossing,u=Depends(user)):
        if len(body.note.strip())<3:raise HTTPException(422,'Review note required')
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM crossings WHERE id=?',(eid,)).fetchone()
            if not row:raise HTTPException(404)
            if not row['finalized']:raise HTTPException(409,'Wait for recognition finalization')
            if row['version']!=body.version:raise HTTPException(409,'Changed record; reload')
            plate=normalize(body.plate);authorization,snapshot=repo.eligibility(c,plate,row['occurred_at'],bool(plate))
            c.execute('UPDATE crossings SET plate=?,status=?,authorization=?,registry_snapshot=?,reason=?,version=version+1 WHERE id=?',(plate,body.decision,authorization,json.dumps(snapshot),'operator reviewed',eid))
            c.execute('INSERT INTO crossing_reviews(crossing_id,user_id,created_at,old_value,new_value,note) VALUES(?,?,?,?,?,?)',(eid,u['id'],utcnow(),json.dumps(dict(row)),json.dumps(body.model_dump()),body.note))
        return {'ok':True}
    @app.put('/api/vehicles/{plate}/validity')
    def validity(plate:str,body:Validity,u=Depends(admin)):
        for v in (body.valid_from,body.valid_until):
            if v is not None and v.tzinfo is None:raise HTTPException(422,'Timezone required')
        if body.valid_from and body.valid_until and body.valid_from>body.valid_until:raise HTTPException(422,'End precedes start')
        values=[v.astimezone(timezone.utc).isoformat() if v else None for v in (body.valid_from,body.valid_until)]
        with store.connect() as c:
            r=c.execute('UPDATE vehicles SET valid_from=?,valid_until=?,updated_at=? WHERE plate=?',(*values,utcnow(),normalize(plate)))
            if not r.rowcount:raise HTTPException(404)
            c.execute('INSERT INTO audit(user_id,created_at,action,detail) VALUES(?,?,?,?)',(u['id'],utcnow(),'registration_validity',json.dumps({'plate':normalize(plate),'interval':values})))
        return {'ok':True}
    @app.get('/api/crossings.csv')
    def export(u=Depends(user)):
        out=io.StringIO();w=csv.writer(out);columns=['id','camera','direction','occurred_at','source_seconds','plate','original_plate','confirmed','authorization','status','reason'];w.writerow(columns)
        with store.connect() as c:
            for r in c.execute('SELECT * FROM crossings ORDER BY occurred_at'):
                w.writerow(["'"+str(r[k]) if str(r[k]).startswith(('=','+','-','@')) else r[k] for k in columns])
        return StreamingResponse(iter([out.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="crossings.csv"'})
