import argparse
import getpass
import os
from pathlib import Path
import sys
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from avacls.storage import Store
from avacls.security import password_hash
p=argparse.ArgumentParser(description='Offline password reset; stop AVACLS first.')
p.add_argument('--username',required=True);a=p.parse_args()
path=Path(os.getenv('AVACLS_DATA_DIR',ROOT/'data'))/'avacls.sqlite3'
if not path.exists(): raise SystemExit('Database does not exist.')
first=getpass.getpass('New password (at least 12 characters): ')
second=getpass.getpass('Repeat password: ')
if len(first)<12 or len(first)>128 or first!=second: raise SystemExit('Password length or confirmation is invalid.')
with Store(path).connect() as c:
    row=c.execute('SELECT id FROM users WHERE username=?',(a.username,)).fetchone()
    if not row: raise SystemExit('User not found.')
    c.execute('UPDATE users SET password=? WHERE id=?',(password_hash(first),row[0]))
    c.execute('DELETE FROM sessions WHERE user_id=?',(row[0],))
    c.execute('INSERT INTO audit(user_id,created_at,action,detail) VALUES(?,?,?,?)',(row[0],datetime.now(timezone.utc).isoformat(),'offline_password_reset',a.username))
print('Password reset. Existing sessions invalidated.')
