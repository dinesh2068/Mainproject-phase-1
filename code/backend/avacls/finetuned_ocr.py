"""Checksum-verified local fine-tuned PP-OCRv5 CTC recognition."""
import hashlib,json
from pathlib import Path
import yaml
from .awiros_ocr import AwirosOCR


def model_characters(folder):
    folder=Path(folder)
    hashes=json.loads((folder/'sha256.json').read_text())
    for name in ['inference.json','inference.pdiparams','inference.yml']:
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=hashes.get(name):
            raise ValueError('Fine-tuned OCR checksum mismatch: '+name)
    cfg=yaml.safe_load((folder/'inference.yml').read_text())
    if cfg['PostProcess']['name']!='CTCLabelDecode':
        raise ValueError('Expected CTC recognition output')
    characters=cfg['PostProcess']['character_dict']
    if not characters or not all(isinstance(c,str) and c for c in characters):
        raise ValueError('Invalid recognition character dictionary')
    return list(characters)+([] if ' ' in characters else [' '])


class FineTunedOCR(AwirosOCR):
    def __init__(self,models,unfold_two_rows=False,device='cpu'):
        name='finetuned_en_PP-OCRv5_mobile_rec'
        characters=model_characters(Path(models)/name)
        super().__init__(models,unfold_two_rows,device,model_subdir=name,character_list=characters)

    def decode(self,probabilities):
        if probabilities.shape[-1]!=len(self.characters):
            raise ValueError('Recognition output and dictionary disagree')
        return super().decode(probabilities)
