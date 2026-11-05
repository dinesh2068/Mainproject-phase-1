from pathlib import Path
import hashlib,json,os
from ultralytics import YOLO
ROOT=Path(__file__).resolve().parents[1]
if __name__=='__main__':
    os.chdir(ROOT/'models');YOLO('yolo26n.pt');p=ROOT/'models/yolo26n.pt'
    (ROOT/'models/vehicle-provenance.json').write_text(json.dumps({'source':'Ultralytics official asset downloader','file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'purpose':'COCO vehicle detector, not trained by AVACLS'},indent=2))
