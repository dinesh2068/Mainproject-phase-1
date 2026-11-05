"""Pure temporal decisions. Source seconds, not inference wall time, drive rules."""
from collections import Counter, deque
import math
from dataclasses import dataclass
from .domain import normalize, valid_plate

@dataclass(frozen=True)
class Gate:
    y: float = .55
    left: float = .05
    right: float = .95
    deadband: float = .035
    stable_frames: int = 2
    max_gap: float = 2.
    increasing_direction: str = 'entry'

    def __post_init__(self):
        if not (0 <= self.left < self.right <= 1 and 0 < self.deadband < self.y < 1-self.deadband):
            raise ValueError('Invalid gate segment or deadband')
        if self.stable_frames < 2 or self.max_gap <= 0 or self.increasing_direction not in ('entry','exit'):
            raise ValueError('Invalid crossing stability/direction')

class Consensus:
    def __init__(self, window=3., votes=3, agreement=.80, confidence=.85, span=.2, syntax=True, min_reading_confidence=0.):
        if not 0<=min_reading_confidence<=1:raise ValueError('Invalid minimum reading confidence')
        self.min_reading_confidence=min_reading_confidence
        if not (window>0 and isinstance(votes,int) and votes>=2 and .5<agreement<=1 and 0<=confidence<=1 and span>=0):
            raise ValueError('Invalid consensus thresholds')
        self.window,self.votes,self.agreement,self.confidence,self.span,self.syntax=window,votes,agreement,confidence,span,syntax
        self.readings=deque(maxlen=60)
        self.last_frame=-1
        self.last_time=None

    def add(self, frame, timestamp, text, score):
        if not math.isfinite(timestamp) or not math.isfinite(score) or not 0<=score<=1:
            raise ValueError('Invalid observation time/confidence')
        if frame <= self.last_frame or (self.last_time is not None and timestamp <= self.last_time):
            raise ValueError('Readings must have distinct increasing frames and timestamps')
        self.last_frame,self.last_time=frame,timestamp
        if score>=self.min_reading_confidence:
            self.readings.append((timestamp,normalize(text),float(score)))

    def result(self, timestamp):
        while self.readings and timestamp-self.readings[0][0] > self.window:
            self.readings.popleft()
        rows=list(self.readings)
        if not rows: return dict(plate='',confirmed=False,count=0,agreement=0.,confidence=0.,span=0.)
        plate,count=Counter(r[1] for r in rows).most_common(1)[0]
        support=[r for r in rows if r[1]==plate]
        confidence=sum(r[2] for r in support)/count
        span=support[-1][0]-support[0][0]
        agreement=count/len(rows)
        confirmed=bool(plate and count>=self.votes and agreement>=self.agreement and confidence>=self.confidence and span+1e-9>=self.span and (not self.syntax or valid_plate(plate)))
        return dict(plate=plate,confirmed=confirmed,count=count,agreement=agreement,confidence=confidence,span=span)

class Crossing:
    def __init__(self, gate):
        self.gate=gate
        self.last=None
        self.anchor=None
        self.side=0
        self.run_side=0
        self.run=0
        self.sequence=0
        self.previous=None
        self.intersection=None

    def update(self, timestamp, x, y):
        g=self.gate
        if self.last is not None and (timestamp<=self.last or timestamp-self.last>g.max_gap):
            self.anchor=None; self.side=0; self.run=0; self.run_side=0
            self.previous=None; self.intersection=None
        self.last=timestamp
        point=(timestamp,x,y)
        # Timestamp the actual observed line crossing, then wait for stable
        # motion beyond the deadband before emitting it. A vehicle may stop
        # inside the band for seconds, so the older stable anchor is unsuitable
        # for interpolation.
        if self.side and (y-g.y)*self.side>0:
            self.intersection=None
        if self.previous is not None and self.side:
            ta,xa,ya=self.previous
            crossed=(ya<g.y<=y) if self.side==-1 else (ya>g.y>=y)
            if crossed:
                fraction=(g.y-ya)/(y-ya)
                self.intersection=dict(source_seconds=ta+fraction*(timestamp-ta),
                    intersection_x=xa+fraction*(x-xa),anchor=list(self.previous),destination=list(point))
        self.previous=point
        side=1 if y>g.y+g.deadband else -1 if y<g.y-g.deadband else 0
        if side==0:
            self.run=0; self.run_side=0
            return None
        self.run=self.run+1 if side==self.run_side else 1
        self.run_side=side
        if self.run<g.stable_frames: return None
        event=None
        if self.anchor is not None and self.side!=side and self.intersection is not None:
            if g.left<=self.intersection['intersection_x']<=g.right:
                self.sequence+=1
                direction=g.increasing_direction if side==1 else ('exit' if g.increasing_direction=='entry' else 'entry')
                event=dict(sequence=self.sequence,direction=direction,**self.intersection,confirmed_seconds=timestamp)
        self.side=side; self.anchor=(timestamp,x,y)
        self.intersection=None
        return event

def associate(plate_box, tracks, containment=.9, margin=.1):
    x1,y1,x2,y2=plate_box
    area=(x2-x1)*(y2-y1)
    if area<=0: return None
    candidates=[]
    for tid,box in tracks.items():
        a,b,c,d=box
        overlap=max(0,min(c,x2)-max(a,x1))*max(0,min(d,y2)-max(b,y1))/area
        if overlap>=containment: candidates.append((overlap,tid))
    candidates.sort(reverse=True)
    if not candidates or (len(candidates)>1 and candidates[0][0]-candidates[1][0]<margin): return None
    return candidates[0][1]
