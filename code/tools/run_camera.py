import argparse,json,os,sys,hashlib
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from avacls.video import run

def main():
    from filelock import FileLock
    p=argparse.ArgumentParser();p.add_argument('--source',required=True,help='Video path, USB index, RTSP URL, or env for AVACLS_CAMERA_SOURCE');p.add_argument('--live',action='store_true');p.add_argument('--paced-replay',action='store_true',help='Replay at source FPS through the live frame buffer');p.add_argument('--config',default=str(ROOT/'config/camera.json'));p.add_argument('--data',default=os.getenv('AVACLS_DATA_DIR',str(ROOT/'data')));p.add_argument('--max-frames',type=int,default=0);p.add_argument('--start-time');p.add_argument('--event-mode',choices=['crossing','entry','exit'],default='crossing');p.add_argument('--run-id');a=p.parse_args()
    if a.live and a.paced_replay:p.error('Choose --live or --paced-replay')
    cfg=json.loads(Path(a.config).read_text());source=os.environ['AVACLS_CAMERA_SOURCE'] if a.source=='env' else a.source
    cfg['event_mode']=a.event_mode
    if source.isdigit():source=int(source)
    if isinstance(source,int) and not a.live:p.error('USB capture requires --live')
    Path(a.data).mkdir(parents=True,exist_ok=True)
    # One worker per data directory: shared preview and session recovery must have one owner.
    with FileLock(str(Path(a.data)/'camera-worker.lock'),timeout=0):
        from avacls.camera_runtime import CameraRuntime
        runtime=CameraRuntime(a.data)
        token=a.run_id or runtime.begin()
        if not a.run_id:(Path(a.data)/'camera.stop').unlink(missing_ok=True)
        try:
            summary=run(ROOT,source,cfg,a.data,a.live,a.max_frames,datetime.fromisoformat(a.start_time) if a.start_time else None,runtime=runtime,run_id=token,paced=a.paced_replay)
            runtime.update(token,state=summary['status'],**{k:v for k,v in summary.items() if k!='status'})
            print(json.dumps(summary,indent=2))
        except Exception:
            runtime.update(token,state='failed',error='Camera processing failed; see data/camera-worker.log for details')
            raise
if __name__=='__main__':main()
