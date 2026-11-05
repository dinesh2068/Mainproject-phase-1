"""Local, persistent model adapter. No network or simulated recognition fallback."""
import os
from pathlib import Path
import numpy as np
from .domain import PlateObservation, select_plate_candidate

class PlateEngine:
    def __init__(self, root: Path, config: dict):
        self.root, self.config = root, config
        self.detector = self.ocr = None

    def load(self):
        if self.config.get('ocr_backend') in ('fastplate','awiros','finetuned_ppocrv5'):
            if not hasattr(self,'fast_engine'):
                from .fast_vision import FastPlateEngine
                self.fast_engine=FastPlateEngine(self.root,self.config)
            self.fast_engine.load()
            return
        if self.detector is not None and self.ocr is not None:
            return
        os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')
        weights = self.root / 'indian_plate_yolo26s.pt'
        det = self.root / 'ocr_text_det_v5'
        rec = self.root / 'stock_en_PP-OCRv5_mobile_rec'
        for p in [weights, det / 'inference.json', rec / 'inference.json']:
            if not p.exists():
                raise RuntimeError(f'Model artifact missing: {p.name}. Restore the bundled models folder.')
        import torch
        torch.set_num_threads(2)
        from ultralytics import YOLO
        from paddleocr import PaddleOCR
        detector = YOLO(str(weights))
        names = list(detector.names.values()) if isinstance(detector.names, dict) else detector.names
        if len(names) != 1 or names[0].lower() not in ('plate', 'license_plate', 'licence_plate', 'number_plate'):
            raise RuntimeError('Expected a single-class plate detector.')
        ocr = PaddleOCR(
            text_detection_model_name='PP-OCRv5_mobile_det', text_detection_model_dir=str(det),
            text_recognition_model_name='en_PP-OCRv5_mobile_rec', text_recognition_model_dir=str(rec),
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_textline_orientation=False, device='cpu', enable_mkldnn=False, cpu_threads=2)
        self.detector, self.ocr = detector, ocr

    def recognize(self, rgb):
        if self.config.get('ocr_backend') in ('fastplate','awiros','finetuned_ppocrv5'):
            self.load()
            return self.fast_engine.recognize(rgb)
        self.load()
        import cv2
        frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = frame.shape[:2]
        result = self.detector.predict(frame, conf=self.config['plate_confidence'],
            imgsz=self.config['image_size'], device='cpu', verbose=False)[0]
        observations = []
        if result.boxes is None:
            return observations
        for box, score in zip(result.boxes.xyxy.cpu().tolist(), result.boxes.conf.cpu().tolist()):
            x1, y1, x2, y2 = map(int, box)
            x1, y1, x2, y2 = max(0,x1), max(0,y1), min(w,x2), min(h,y2)
            if x2 <= x1 or y2 <= y1:
                continue
            padding=float(self.config.get('crop_padding',0))
            if not 0<=padding<=.25:raise ValueError('crop_padding must be in [0,.25]')
            px,py=round((x2-x1)*padding),round((y2-y1)*padding)
            crop = frame[max(0,y1-py):min(h,y2+py),max(0,x1-px):min(w,x2+px)]
            warnings, parts = [], []
            if x2-x1 < 80 or y2-y1 < 24:
                warnings.append('Small plate crop')
            if cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var() < 40:
                warnings.append('Possible blur')
            if x2-x1 >= 16 and y2-y1 >= 8:
                target=int(self.config.get('ocr_min_crop_height',0))
                if not 0<=target<=256:raise ValueError('ocr_min_crop_height must be in [0,256]')
                if target and crop.shape[0]<target:
                    scale=min(target/crop.shape[0],4.)
                    crop=cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
                output = list(self.ocr.predict(crop))
                if output:
                    r = output[0]
                    for txt, conf, poly in zip(r['rec_texts'], r['rec_scores'], r['rec_polys']):
                        p = np.asarray(poly)
                        if txt.strip():
                            parts.append((float(p[:,1].mean()), float(p[:,0].min()), txt, float(conf), float(np.ptp(p[:,1]))))
            lines = []
            for part in sorted(parts):
                if lines and abs(part[0]-np.mean([p[0] for p in lines[-1]])) < max(part[4]*.6,4):
                    lines[-1].append(part)
                else:
                    lines.append([part])
            ordered = [p for row in lines for p in sorted(row, key=lambda p:p[1])]
            candidate, ocr_score, candidate_warnings = select_plate_candidate([(p[2],p[3]) for p in ordered])
            warnings.extend(candidate_warnings)
            observations.append(PlateObservation(' '.join(p[2] for p in ordered), float(score),
                ocr_score, [x1,y1,x2,y2], warnings, candidate))
        return observations
