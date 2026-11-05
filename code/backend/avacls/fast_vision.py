"""Local plate-specific OCR; no text-region detector and no runtime downloads."""
import os
# Must precede ONNX Runtime initialization: disables its uploader and device ID.
os.environ['ORT_DISABLE_TELEMETRY']='1'
from pathlib import Path
import numpy as np
from .domain import PlateObservation,normalize

class FastPlateEngine:
    def __init__(self,root,config):
        self.root,self.config=Path(root),config
        self.detector=self.ocr=None
    def load(self):
        if self.detector is not None:return
        import torch
        from ultralytics import YOLO
        torch.set_num_threads(2)
        self.device=self.config.get('plate_device','cpu')
        if self.device!='cpu' and not torch.cuda.is_available():
            raise RuntimeError('Plate detector requested CUDA but CUDA is unavailable')
        self.detector=YOLO(str(self.root/'indian_plate_yolo26s.pt'))
        if self.config.get('ocr_backend')=='finetuned_ppocrv5':
            from .finetuned_ocr import FineTunedOCR
            self.ocr=FineTunedOCR(self.root,self.config.get('unfold_two_rows',False),self.config.get('ocr_device','cpu'))
            return
        if self.config.get('ocr_backend')=='awiros':
            from .awiros_ocr import AwirosOCR
            self.ocr=AwirosOCR(self.root,self.config.get('unfold_two_rows',False),self.config.get('ocr_device','cpu'))
            return
        import onnxruntime as ort
        ort.disable_telemetry_events()
        from fast_plate_ocr import LicensePlateRecognizer
        opts=ort.SessionOptions();opts.intra_op_num_threads=2;opts.inter_op_num_threads=1
        self.ocr=LicensePlateRecognizer(onnx_model_path=self.root/'fastplate/cct_s_v2_global.onnx',plate_config_path=self.root/'fastplate/cct_s_v2_global_plate_config.yaml',providers=['CPUExecutionProvider'],sess_options=opts)
    def recognize(self,rgb):
        self.load()
        import cv2
        h,w=rgb.shape[:2]
        pred=self.detector.predict(cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR),conf=self.config['plate_confidence'],imgsz=self.config['image_size'],device=self.device,half=bool(self.config.get('half',False) and self.device!='cpu'),verbose=False)[0]
        if pred.boxes is None:return []
        crops=[];meta=[];padding=float(self.config.get('crop_padding',.04))
        if not 0<=padding<=.25:raise ValueError('Invalid plate crop padding')
        for box,score in zip(pred.boxes.xyxy.cpu().tolist(),pred.boxes.conf.cpu().tolist()):
            x1,y1,x2,y2=map(int,box);x1,y1,x2,y2=max(0,x1),max(0,y1),min(w,x2),min(h,y2)
            minimum=self.config.get('min_crop_width',16)
            if (x2-x1)/max(1,y2-y1)<2.8:minimum=self.config.get('min_two_row_width',minimum)
            if x2-x1<minimum or y2-y1<self.config.get('min_crop_height',8):continue
            px,py=round((x2-x1)*padding),round((y2-y1)*padding)
            crop=rgb[max(0,y1-py):min(h,y2+py),max(0,x1-px):min(w,x2+px)]
            crops.append(crop);meta.append(([x1,y1,x2,y2],float(score)))
        if not crops:return []
        predictions=self.ocr.run(crops,return_confidence=True)
        observations=[]
        for r,(box,score) in zip(predictions,meta):
            text=normalize(r.plate);probs=np.asarray(r.char_probs,dtype=float)[:len(r.plate)]
            confidence=float(probs.mean()) if len(probs) else 0.
            warnings=[]
            if getattr(r,'layout',None):warnings.append(r.layout)
            if box[2]-box[0]<80 or box[3]-box[1]<24:warnings.append('Small plate crop')
            observations.append(PlateObservation(r.plate,score,confidence,box,warnings,text))
        return observations
