"""Camera/file worker. Crossings persist independently of plate readability."""
import json, time, uuid, hashlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
from collections import defaultdict
from dataclasses import asdict
from threading import Thread,Condition,Event
from .temporal import Gate,Crossing,Consensus,associate
from .storage import Store
from .live_store import LiveStore
from .vision import PlateEngine
from .pipeline import recognize_observations

from .capture import Capture
from .sampling import OCRSchedule

class Decisions:
    def __init__(self,repo,sid,cfg,origin,media):
        self.repo,self.sid,self.cfg,self.origin,self.media=repo,sid,cfg,origin,media
        self.gate=Gate(**cfg['gate']);self.states={};self.pending=[]
    @staticmethod
    def _remember(holder,result,t,readings):
        if not result['confirmed']:return
        snapshot=dict(decision=dict(result),checked_seconds=t,readings=list(readings))
        qualified=holder['qualified']
        if qualified is None or qualified['decision']['plate']==result['plate']:
            holder['qualified']=snapshot
        elif not any(c['decision']['plate']==result['plate'] for c in holder['conflicts']):
            holder['conflicts'].append(snapshot)
    def update(self,i,t,tracks,observations,frame):
        import cv2
        assigned=defaultdict(list)
        for p in observations:
            tid=associate(p.box,tracks)
            if tid is not None:assigned[tid].append(p)
        for tid,box in tracks.items():
            st=self.states.setdefault(tid,dict(vote=Consensus(**self.cfg['consensus']),motion=Crossing(self.gate),seen=t,readings=[],qualified=None,conflicts=[]))
            if t-st['seen']>self.gate.max_gap:
                st['vote']=Consensus(**self.cfg['consensus']);st['readings']=[];st['qualified']=None;st['conflicts']=[]
            st['seen']=t
            if len(assigned[tid])==1:
                p=assigned[tid][0];text=p.candidate_text if p.candidate_text is not None else p.raw_text
                st['vote'].add(i,t,text,p.ocr_confidence)
                st['readings'].append(dict(frame=i,seconds=t,text=text,raw=p.raw_text,confidence=p.ocr_confidence,box=p.box))
                st['readings']=st['readings'][-60:]
            h,w=frame.shape[:2]
            crossing=st['motion'].update(t,(box[0]+box[2])/2/w,box[3]/h)
            result=st['vote'].result(t)
            # Recognition normally precedes the crossing. Keep its qualified
            # evidence only within this continuous vehicle track; a tracking
            # gap clears it and conflicting confirmations require review.
            self._remember(st,result,t,st['readings'])
            if assigned[tid] or not st.get('published'):
                name=uuid.uuid4().hex+'.jpg'
                st['published']=True
                temp=self.media/('tmp-'+name)
                evidence_frame=frame.copy()
                x1,y1,x2,y2=map(int,box)
                cv2.rectangle(evidence_frame,(x1,y1),(x2,y2),(0,200,0),3)
                cv2.putText(evidence_frame,f'Track {tid}',(max(0,x1),max(24,y1)),0,.8,(0,200,0),2)
                if not cv2.imwrite(str(temp),evidence_frame):raise IOError('Detection evidence could not be saved')
                temp.replace(self.media/name)
                evidence=dict(image=name,frame=i,box=box,readings=list(st['readings']),decision=result)
                when=(self.origin+timedelta(seconds=t)).isoformat()
                self.repo.detection(self.sid,tid,when,t,result,evidence)
                mode=self.cfg.get('event_mode','crossing')
                if mode in ('entry','exit') and result['confirmed']:
                    try:self.repo.record_detection(self.sid,tid,mode)
                    except ValueError as exc:
                        result['recording_message']=str(exc)
                        self.repo.detection(self.sid,tid,when,t,result,evidence)
            if crossing and self.cfg.get('event_mode','crossing')=='crossing':
                name=uuid.uuid4().hex+'.jpg'
                if not cv2.imwrite(str(self.media/name),frame):raise IOError('Crossing evidence could not be saved')
                result=st['vote'].result(t)
                evidence=dict(image=name,frame=i,box=box,crossing=crossing,readings=list(st['readings']),initial_candidate=result['plate'])
                when=(self.origin+timedelta(seconds=crossing['source_seconds'])).isoformat()
                eid=self.repo.stage(self.sid,self.cfg['camera'],tid,crossing,when,evidence)
                self.pending.append(dict(id=eid,tid=tid,deadline=t+self.cfg['grace_seconds'],evidence=evidence,qualified=st['qualified'],conflicts=list(st['conflicts'])))
        self.flush(t)
        active={p['tid'] for p in self.pending}
        for tid in list(self.states):
            if t-self.states[tid]['seen']>10 and tid not in active:del self.states[tid]
    def flush(self,t,ending=False):
        for p in list(self.pending):
            st=self.states[p['tid']]
            current=st['vote'].result(t)
            # Expiry prevents stale readings from supplying NEW votes. It must
            # not erase a completed confirmation for an already observed
            # crossing while its vehicle leaves the camera view.
            self._remember(p,current,t,st['readings'])
            if ending or t>=p['deadline']:
                qualified=p['qualified']
                r=dict(qualified['decision']) if qualified else current
                initial=p['evidence']['initial_candidate']
                early=ending and t<p['deadline'];changed=bool(initial and r['plate'] and initial!=r['plate'])
                evidence={**p['evidence'],'readings':list(st['readings']),'decision':r,'decision_at_finalization':current,
                    'qualified_confirmation':qualified,'conflicting_confirmations':list(p['conflicts']),
                    'decision_basis':'retained_qualified_confirmation' if qualified and not current['confirmed'] else 'current_consensus',
                    'finalized_seconds':t,'ended_before_grace':early}
                self.repo.finalize(p['id'],r,evidence,force_review=bool(p['conflicts']) or changed or (early and qualified is None));self.pending.remove(p)

def run(root,source,cfg,data,live=False,max_frames=0,start_time=None,runtime=None,run_id=None,paced=False):
    import cv2,torch,numpy as np
    from ultralytics import YOLO
    root=Path(root);data=Path(data);data.mkdir(parents=True,exist_ok=True);media=data/'media';media.mkdir(exist_ok=True)
    Gate(**cfg['gate']);Consensus(**cfg['consensus'])
    if cfg['ocr_every']<1 or cfg['grace_seconds']<0:raise ValueError('Invalid OCR cadence/grace')
    schedule=OCRSchedule(cfg['ocr_every'],cfg.get('ocr_interval_seconds'))
    model=root/cfg['vehicle_model']
    if not model.is_file():raise FileNotFoundError('Run tools/download_vehicle_model.py first')
    if cfg['device']!='cpu' and not torch.cuda.is_available():raise RuntimeError('CUDA unavailable')
    torch.set_num_threads(4);cv2.setNumThreads(2)
    detector=YOLO(str(model));plate_cfg=json.loads((root/'config.json').read_text());plate_cfg.update(cfg.get('plate_overrides',{}));plate=PlateEngine(root/'models',plate_cfg);plate.load()
    if cfg.get('warmup',False):
        if runtime:runtime.update(run_id,state='warming_models')
        dummy=np.zeros((360,640,3),dtype=np.uint8)
        detector.track(dummy,persist=True,agnostic_nms=True,tracker=str(root/'config/botsort.yaml'),classes=[2,3,5,7],conf=cfg['vehicle_confidence'],imgsz=cfg['image_size'],device=cfg['device'],half=bool(cfg.get('half',False) and cfg['device']!='cpu'),verbose=False)
        if detector.predictor and hasattr(detector.predictor,'trackers'):
            for tracker in detector.predictor.trackers:tracker.reset()
        recognize_observations(dummy,plate)
        if hasattr(plate,'fast_engine'):
            plate.fast_engine.ocr.run(np.zeros((48,192,3),dtype=np.uint8))
    if runtime:runtime.update(run_id,state='opening_source')
    source_hash=hashlib.sha256(Path(source).read_bytes()).hexdigest() if not live else None
    repo=LiveStore(Store(data/'avacls.sqlite3'));cap=Capture(source,live,data/"camera.stop",paced=paced);origin=start_time or cap.origin
    if origin.tzinfo is None:cap.close();raise ValueError('Start time requires timezone')
    provenance={**cfg,'source_kind':'live' if live else 'recorded','timestamp_basis':'capture_clock' if live else ('provided_start' if start_time else 'replay_clock_not_original_capture')}
    provenance.update(replay_mode='paced_latest_frame' if paced else ('live' if live else 'sequential'),plate_config=plate_cfg)
    if not live:provenance['source_sha256']=source_hash
    sid=repo.session(cfg['camera'],provenance,origin.isoformat());output=data/'runs'/sid;output.mkdir(parents=True)
    if runtime:runtime.update(run_id,state='processing',session_id=sid)
    (output/'config.json').write_text(json.dumps(provenance,indent=2))
    dec=Decisions(repo,sid,cfg,origin,media);generation=0;count=0;last=0.;timings=[];start=time.perf_counter();status='completed';latencies=[]
    recorded_rows=[] if not live else None
    try:
        with (output/'observations.jsonl').open('w') as log:
            for i,t,frame,gen in cap:
                if (data/"camera.stop").exists():
                    status="stopped";break
                if gen!=generation:
                    dec.flush(last,True);repo.finish(sid,'reconnected',{'frames':count})
                    sid=repo.session(cfg['camera'],provenance,origin.isoformat());dec=Decisions(repo,sid,cfg,origin,media)
                    if runtime:runtime.update(run_id,session_id=sid)
                    if detector.predictor:
                        for tr in detector.predictor.trackers:tr.reset()
                    generation=gen
                tick=time.perf_counter()
                pred=detector.track(frame,persist=True,agnostic_nms=True,tracker=str(root/'config/botsort.yaml'),classes=[2,3,5,7],conf=cfg['vehicle_confidence'],imgsz=cfg['image_size'],device=cfg['device'],half=bool(cfg.get('half',False) and cfg['device']!='cpu'),verbose=False)[0]
                tracks={}
                if pred.boxes is not None and pred.boxes.id is not None:tracks={int(tid):list(map(float,b)) for tid,b in zip(pred.boxes.id.cpu().tolist(),pred.boxes.xyxy.cpu().tolist())}
                obs=recognize_observations(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB),plate) if schedule.due(count,t) else []
                dec.update(i,t,tracks,obs,frame);elapsed=time.perf_counter()-tick;timings.append(elapsed)
                latency=max(0,time.monotonic()-cap.clock-t) if cap.buffered else None
                if latency is not None:latencies.append(latency)
                line=json.dumps(dict(session=sid,frame=i,seconds=t,tracks=tracks,observations=[asdict(p) for p in obs],processing_seconds=elapsed,completion_latency_seconds=latency))+'\n'
                log.write(line);log.flush()
                if recorded_rows is not None:recorded_rows.append(line)
                preview=frame.copy();h,w=frame.shape[:2];g=dec.gate
                cv2.line(preview,(int(g.left*w),int(g.y*h)),(int(g.right*w),int(g.y*h)),(255,255,255),3)
                for tid,b in tracks.items():
                    x1,y1,x2,y2=map(int,b);cv2.rectangle(preview,(x1,y1),(x2,y2),(0,200,0),2);cv2.putText(preview,str(tid),(x1,y1+24),0,.8,(0,200,0),2)
                ok,encoded=cv2.imencode('.jpg',preview)
                if runtime and ok:runtime.update(run_id,jpeg=encoded.tobytes(),state='stopping' if (data/'camera.stop').exists() else 'processing',frames=count+1,source_seconds=t,dropped_frames=cap.dropped)
                count+=1;last=t
                if max_frames and count>=max_frames:break
    except KeyboardInterrupt:status='stopped'
    except Exception:status='failed';raise
    finally:
        if (data/'camera.stop').exists() and status=='completed':status='stopped'
        cap.close();dec.flush(last,True)
        summary=dict(frames=count,source_seconds=last,wall_seconds=time.perf_counter()-start,dropped_frames=cap.dropped,device=cfg['device'],status=status,captured_frames=cap.captured,replay_mode=provenance['replay_mode'])
        summary['processed_fps']=count/summary['wall_seconds'] if summary['wall_seconds'] else 0
        if latencies:summary.update(completion_latency_p50=float(np.percentile(latencies,50)),completion_latency_p95=float(np.percentile(latencies,95)))
        if timings:summary.update(processing_p50=float(np.percentile(timings,50)),processing_p95=float(np.percentile(timings,95)))
        if recorded_rows is not None:
            complete_log=output/'observations.complete.tmp'
            complete_log.write_text(''.join(recorded_rows))
            complete_log.replace(output/'observations.jsonl')
            summary['logged_frames']=len(recorded_rows)
            summary['observations_sha256']=hashlib.sha256((output/'observations.jsonl').read_bytes()).hexdigest()
        repo.finish(sid,status,summary);(output/'summary.json').write_text(json.dumps(summary,indent=2))
    return summary
