"""Shared contract: future video adapters emit the same PlateObservation objects."""
from dataclasses import dataclass
from typing import Protocol
import re
import numpy as np


def normalize(text: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', text.upper())


def valid_plate(text: str) -> bool:
    # Format screening, not government registration verification.
    return bool(re.fullmatch(r'(?:[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}|[0-9]{2}BH[0-9]{4}[A-Z]{1,2})', text))


@dataclass
class PlateObservation:
    raw_text: str
    detection_confidence: float
    ocr_confidence: float
    box: list[int]
    warnings: list[str]
    candidate_text: str | None = None


class RecognitionEngine(Protocol):
    def recognize(self, rgb: np.ndarray) -> list[PlateObservation]: ...


def select_plate_candidate(fragments: list[tuple[str, float]]):
    """Select a unique supported reading from contiguous OCR fragments.

    Never substitute characters or manufacture a plate. Extra lettering and
    conflicting candidates remain visible as review warnings.
    """
    tokens = [(normalize(text),score) for text,score in fragments if normalize(text)]
    candidates = {}
    for start in range(len(tokens)):
        joined=''
        scores=[]
        for end in range(start,min(start+12,len(tokens))):
            joined+=tokens[end][0]; scores.append(tokens[end][1])
            if len(joined)>12: break
            if valid_plate(joined):
                candidates[joined]=max(candidates.get(joined,0),min(scores))
    original=''.join(t[0] for t in tokens)
    if len(candidates)==1:
        plate,score=next(iter(candidates.items()))
        warnings=['Extra OCR text excluded from proposed plate; verify the crop'] if plate!=original else []
        return plate,score,warnings
    if len(candidates)>1:
        return original,min((t[1] for t in tokens),default=0),['Conflicting plate candidates; manual verification required']
    return original,min((t[1] for t in tokens),default=0),[]

