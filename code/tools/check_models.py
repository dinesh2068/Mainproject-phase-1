"""Explicit model load and inference check. Uses a supplied image or a blank input."""
from pathlib import Path
import argparse
import json
import sys
import hashlib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from avacls.vision import PlateEngine
import numpy as np
from PIL import Image

p=argparse.ArgumentParser();p.add_argument('--image',type=Path);p.add_argument('--camera-config',type=Path);a=p.parse_args()
checksums=json.loads((ROOT/'models/checksums.json').read_text())
vehicle=json.loads((ROOT/'models/vehicle-provenance.json').read_text())
checksums[vehicle['file']]=vehicle['sha256']
for name,expected in checksums.items():
    actual=hashlib.sha256((ROOT/'models'/name).read_bytes()).hexdigest()
    if actual!=expected:raise SystemExit('Model checksum mismatch: '+name)
print('All model checksums match.')
config=json.loads((ROOT/'config.json').read_text())
if a.camera_config:config.update(json.loads(a.camera_config.read_text()).get('plate_overrides',{}))
engine=PlateEngine(ROOT/'models',config)
image=np.asarray(Image.open(a.image).convert('RGB')) if a.image else np.full((480,640,3),255,dtype=np.uint8)
result=engine.recognize(image)
# Exercise OCR even if the blank model smoke input has no detected plates.
if not a.image:
    if hasattr(engine,'fast_engine'):engine.fast_engine.ocr.run(np.zeros((48,192,3),dtype=np.uint8))
    else:list(engine.ocr.predict(image))
print(json.dumps({'input':str(a.image) if a.image else 'blank smoke input','observations':[vars(r) for r in result]},indent=2))
