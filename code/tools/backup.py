"""Offline snapshot/restore: stop the server to keep images and DB consistent."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def backup(source,destination):
    source=Path(source).resolve(); destination=Path(destination).resolve()
    if not (source/'avacls.sqlite3').exists(): raise ValueError('Database not found.')
    if destination.exists(): raise ValueError('Destination already exists; choose a new archive filename.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot=Path(tmp)/'avacls.sqlite3'
        with sqlite3.connect(source/'avacls.sqlite3') as src, sqlite3.connect(snapshot) as dst:
            src.backup(dst)
            dst.execute('DELETE FROM sessions'); dst.commit()
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot,'avacls.sqlite3')
            for f in (source/'media').glob('*.jpg'): z.write(f,'media/'+f.name)
            z.writestr('backup.json',json.dumps({'version':1,'created_at':datetime.now(timezone.utc).isoformat(),'sessions':'removed'}))
    return destination

def restore(archive,destination):
    destination=Path(destination).resolve()
    if destination.exists(): raise ValueError('Restore destination must not exist. This prevents overwriting current data.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        stage=Path(tmp)/'restore'; stage.mkdir()
        with zipfile.ZipFile(archive) as z:
            if 'avacls.sqlite3' not in z.namelist() or 'backup.json' not in z.namelist(): raise ValueError('Not an AVACLS backup.')
            for item in z.infolist():
                target=(stage/item.filename).resolve()
                if not target.is_relative_to(stage.resolve()) or item.is_dir(): raise ValueError('Unsafe archive entry.')
                if item.file_size>500_000_000: raise ValueError('Unexpectedly large archive entry.')
                target.parent.mkdir(parents=True,exist_ok=True)
                with z.open(item) as src, open(target,'wb') as dst: shutil.copyfileobj(src,dst)
        with sqlite3.connect(stage/'avacls.sqlite3') as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Database integrity check failed.')
            c.execute('DELETE FROM sessions'); c.commit()
        stage.rename(destination)
    return destination

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['backup','restore'])
    p.add_argument('--data',type=Path,default=Path(os.getenv('AVACLS_DATA_DIR',ROOT/'data')))
    p.add_argument('--archive',type=Path)
    p.add_argument('--destination',type=Path)
    a=p.parse_args()
    try:
        if a.action=='backup':
            target=a.archive or ROOT/'backups'/('avacls-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.zip')
            print(backup(a.data,target))
        else:
            if not a.archive or not a.destination: p.error('restore requires --archive and --destination')
            print(restore(a.archive,a.destination))
    except (ValueError,FileNotFoundError) as e: raise SystemExit(str(e))
if __name__=='__main__': main()
