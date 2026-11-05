"""One local server serves the compiled React application and Python API."""
import argparse
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--port',type=int,default=8000)
    p.add_argument('--dev',action='store_true',help='Allow the localhost Vite dev origin')
    a=p.parse_args()
    if not (ROOT/'frontend/dist/index.html').is_file():
        raise SystemExit('Frontend build missing. Run: python tools/setup.py')
    if a.dev: os.environ['AVACLS_DEV']='1'
    print(f'AVACLS: http://127.0.0.1:{a.port}  |  Ctrl+C to stop')
    import uvicorn
    uvicorn.run('avacls.app:create_app',factory=True,host='127.0.0.1',port=a.port,workers=1)

if __name__=='__main__': main()
