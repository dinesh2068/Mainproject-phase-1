"""Input adapters terminate at RGB frames; shared recognition is source-independent."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import numpy as np
from PIL import Image

@dataclass
class InputFrame:
    rgb: np.ndarray
    source: str
    captured_at: str | None = None  # Never invent a capture time from upload time.

class InputAdapter(Protocol):
    def read(self) -> InputFrame: ...

@dataclass
class ImageInput:
    path: Path
    def read(self) -> InputFrame:
        with Image.open(self.path) as source:
            return InputFrame(np.asarray(source.convert('RGB')), 'image')
