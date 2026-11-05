"""Select OCR opportunities using elapsed source time or legacy frame cadence."""
import math
class OCRSchedule:
    def __init__(self,every=3,interval=None):
        if not isinstance(every,int) or every<1:raise ValueError('Invalid frame cadence')
        if interval is not None and (not math.isfinite(interval) or interval<=0):raise ValueError('Invalid interval')
        self.every,self.interval,self.last=every,interval,None
    def due(self,count,timestamp):
        if not math.isfinite(timestamp):raise ValueError('Invalid timestamp')
        if self.interval is None:return count%self.every==0
        if self.last is None or timestamp<self.last or timestamp-self.last+1e-9>=self.interval:
            self.last=timestamp
            return True
        return False
