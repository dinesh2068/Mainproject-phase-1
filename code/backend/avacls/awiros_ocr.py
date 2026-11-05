"""Local Awiros Indian plate reader, using the published PP-OCR architecture."""
from pathlib import Path
from types import SimpleNamespace
import numpy as np


class AwirosOCR:
    def __init__(self, models, unfold_two_rows=False, device='cpu', model_subdir='awiros', character_list=None):
        import paddle
        self.unfold_two_rows = unfold_two_rows
        models = Path(models)
        folder = models / model_subdir
        cfg=paddle.inference.Config(str(folder/'inference.json'),str(folder/'inference.pdiparams'))
        if device == 'cpu':
            cfg.disable_gpu()
            cfg.set_cpu_math_library_num_threads(2)
            cfg.enable_mkldnn()
        else:
            if not str(device).isdigit():
                raise ValueError('OCR device must be cpu or a nonnegative GPU index')
            if not paddle.is_compiled_with_cuda():
                raise RuntimeError('GPU OCR requires a CUDA-enabled Paddle installation')
            index = int(device)
            if index >= paddle.device.cuda.device_count():
                raise RuntimeError('Requested OCR GPU is unavailable')
            cfg.enable_use_gpu(256, index)
        cfg.disable_glog_info()
        self.predictor=paddle.inference.create_predictor(cfg)
        self.input=self.predictor.get_input_handle(self.predictor.get_input_names()[0])
        self.output=self.predictor.get_output_handle(self.predictor.get_output_names()[0])
        self.characters=['']+list(character_list) if character_list is not None else ['']+(folder/'en_dict.txt').read_text().splitlines()+[' ']

    def decode(self, probabilities):
        results=[]
        for row in probabilities:
            indices=row.argmax(axis=1)
            selected=[i for i,index in enumerate(indices) if index!=0 and (i==0 or index!=indices[i-1])]
            selected=[i for i in selected if self.characters[indices[i]].strip()]
            text=''.join(self.characters[indices[i]] for i in selected).upper()
            scores=[float(row[i,indices[i]]) for i in selected]
            results.append(SimpleNamespace(plate=text,char_probs=scores))
        return results

    @staticmethod
    def preprocess(rgb):
        import cv2
        h, w = rgb.shape[:2]
        width = max(1, min(int(w * 48 / h), 320))
        resized = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), (width, 48))
        canvas = np.zeros((48,320,3), dtype=np.uint8)
        canvas[:,:width] = resized
        return ((canvas.astype(np.float32)/255. - .5)/.5).transpose(2,0,1)

    @staticmethod
    def unfold(rgb):
        """Rearrange existing top/bottom pixels into reading order."""
        import cv2
        h,w=rgb.shape[:2]
        if h<2:raise ValueError('Two-row crop must have at least two rows')
        cut=h//2
        top=cv2.resize(rgb[:cut],(w,h-cut))
        return np.concatenate([top,np.full((h-cut,4,3),255,dtype=rgb.dtype),rgb[cut:]],axis=1)

    def run(self, crops, return_confidence=False):
        if isinstance(crops,np.ndarray): crops=[crops]
        if not crops: return []
        variants=list(crops); pairs=[]
        for i,c in enumerate(crops):
            if self.unfold_two_rows and c.shape[1]/c.shape[0]<2.8:
                pairs.append((i,len(variants)));variants.append(self.unfold(c))
        batch=np.stack([self.preprocess(c) for c in variants])
        self.input.reshape(batch.shape)
        self.input.copy_from_cpu(batch)
        self.predictor.run()
        results=self.decode(self.output.copy_to_cpu())
        from .domain import valid_plate
        def rank(r):return (valid_plate(r.plate),float(np.mean(r.char_probs)) if r.char_probs else 0.)
        for original,alternate in pairs:
            if rank(results[alternate])>rank(results[original]):
                results[original]=results[alternate]
                results[original].layout='two_rows_unfolded'
        return results[:len(crops)]
