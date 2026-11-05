"""Sequential, live latest-frame, and paced replay capture."""
import time
from datetime import datetime,timezone
from threading import Thread,Condition,Event

class Capture:
    def __init__(self,source,live=False,stop_path=None,paced=False):
        if live and paced: raise ValueError("Choose live capture or paced replay")
        if paced and (not isinstance(source,str) or "://" in source): raise ValueError("Paced replay requires a local file")
        import cv2
        self.stop_path=stop_path;self.paced=paced;self.buffered=live or paced;self.captured=0
        self.cv2=cv2;self.source=source;self.live=live;self.stop=Event();self.cv=Condition()
        self.latest=None;self.done=False;self.error=None;self.dropped=0;self.generation=0
        self.cap=self.open();self.fps=self.cap.get(cv2.CAP_PROP_FPS)
        if not live and not 0<self.fps<1000:
            self.cap.release();raise ValueError('File FPS unavailable')
        self.origin=datetime.now(timezone.utc);self.clock=time.monotonic();self.thread=None
        if self.buffered:self.thread=Thread(target=self.pump,daemon=True);self.thread.start()
    def open(self):
        c=self.cv2
        cap=c.VideoCapture(self.source,c.CAP_FFMPEG,[c.CAP_PROP_OPEN_TIMEOUT_MSEC,5000,c.CAP_PROP_READ_TIMEOUT_MSEC,5000]) if isinstance(self.source,str) and '://' in self.source else c.VideoCapture(self.source)
        if not cap.isOpened():cap.release();raise RuntimeError('Cannot open camera/video source')
        return cap
    def pump(self):
        i=0
        try:
            while not self.stop.is_set() and not (self.stop_path and self.stop_path.exists()):
                if self.paced and self.stop.wait(max(0,self.clock+i/self.fps-time.monotonic())):break
                ok,frame=self.cap.read()
                if not ok:
                    if self.paced:break
                    self.cap.release()
                    if self.stop.wait(1):break
                    self.cap=self.open();self.generation+=1;continue
                with self.cv:
                    if self.latest is not None:self.dropped+=1
                    self.latest=(i,i/self.fps if self.paced else time.monotonic()-self.clock,frame,self.generation);i+=1;self.captured+=1;self.cv.notify_all()
        except Exception:self.error='Camera disconnected; restart worker after checking source'
        finally:
            with self.cv:self.done=True;self.cv.notify_all()
    def __iter__(self):
        if not self.buffered:
            i=0
            while not self.stop.is_set() and not (self.stop_path and self.stop_path.exists()):
                ok,f=self.cap.read()
                if not ok:break
                self.captured+=1
                yield i,i/self.fps,f,0;i+=1
        else:
            while not self.stop.is_set() and not (self.stop_path and self.stop_path.exists()):
                with self.cv:
                    self.cv.wait_for(lambda:self.latest is not None or self.done,timeout=6)
                    item=self.latest;self.latest=None;done=self.done
                if item is not None:yield item
                elif done:
                    if self.error:raise RuntimeError(self.error)
                    break
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=7)
        self.cap.release()

