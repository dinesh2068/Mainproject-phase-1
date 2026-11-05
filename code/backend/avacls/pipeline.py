"""Shared frame recognition and evidence generation, independent of HTTP/input mode."""
from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageDraw
import uuid
from .domain import normalize, valid_plate, RecognitionEngine
from .inputs import InputFrame

@dataclass
class ProcessedObservation:
    id: str
    raw_text: str
    plate: str
    detection_confidence: float
    ocr_confidence: float
    box: list[int]
    warnings: list[str]
    format_valid: bool
    crop_path: str


def recognize_observations(rgb, engine):
    """One recognition entry point for still images and tracked video frames."""
    return engine.recognize(rgb)


def recognize_frame(frame: InputFrame, engine: RecognitionEngine, media: Path,
                    job_id: str, ocr_threshold: float):
    predictions = recognize_observations(frame.rgb, engine)
    image = Image.fromarray(frame.rgb)
    annotated = image.copy(); draw = ImageDraw.Draw(annotated)
    observations=[]
    for p in predictions:
        oid=uuid.uuid4().hex
        text=normalize(p.candidate_text if p.candidate_text is not None else p.raw_text)
        issues=list(p.warnings)
        if not text: issues.append('No readable text')
        if not valid_plate(text): issues.append('Unsupported or invalid plate format')
        if p.ocr_confidence < ocr_threshold: issues.append('Low OCR confidence')
        crop=oid+'.jpg'
        image.crop(tuple(p.box)).save(media/crop, quality=90)
        draw.rectangle(tuple(p.box),outline='#19bd97',width=3)
        draw.text((p.box[0],max(0,p.box[1]-14)),text or 'Unread',fill='#19bd97')
        observations.append(ProcessedObservation(oid,p.raw_text,text,p.detection_confidence,
            p.ocr_confidence,p.box,issues,valid_plate(text),crop))
    path=job_id+'-annotated.jpg'; annotated.save(media/path,quality=90)
    return observations,path
