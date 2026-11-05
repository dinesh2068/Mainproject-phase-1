"""Windows/Linux local setup; no Docker, database installer, or global pip changes."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT=Path(__file__).resolve().parents[1]

def run(args):
    subprocess.run([str(a) for a in args],cwd=ROOT,check=True)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--web-only',action='store_true',help='Install UI/API only; recognition will explicitly fail until AI dependencies are installed.')
    p.add_argument('--rebuild-web',action='store_true',help='Rebuild the included React distribution using npm ci.')
    a=p.parse_args()
    if not ((3,11)<=sys.version_info[:2]<(3,13)):
        raise SystemExit('Use Python 3.11 or 3.12 (64-bit) for model compatibility.')
    env=ROOT/'.venv'
    if not env.exists(): venv.EnvBuilder(with_pip=True).create(env)
    python=env/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    run([python,'-m','pip','install','--upgrade','pip'])
    if not a.web_only:
        # Explicit CPU wheels avoid downloading CUDA libraries for this CPU pilot.
        run([python,'-m','pip','install','torch==2.14.1','torchvision==0.29.1','--index-url','https://download.pytorch.org/whl/cpu'])
    req='requirements.txt' if a.web_only else 'requirements-video.txt'
    run([python,'-m','pip','install','-r',ROOT/'backend'/req])
    run([python,'-m','pip','check'])
    if a.rebuild_web or not (ROOT/'frontend/dist/index.html').exists():
        npm=shutil.which('npm')
        if not npm: raise SystemExit('Install Node.js 22.12+ to rebuild the frontend, or use the included frontend/dist.')
        run([npm,'--prefix',ROOT/'frontend','ci','--no-audit','--no-fund'])
        run([npm,'--prefix',ROOT/'frontend','run','build'])
    print('\nSetup complete. Start using:')
    print(f'  "{python}" tools/start.py')

if __name__=='__main__': main()
