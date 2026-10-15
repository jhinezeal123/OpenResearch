"""One-time local import. No source directory is saved as a runtime dependency."""
import argparse
import json
from pathlib import Path
import re
import shutil

def import_profiles(source, home):
    source=Path(source).resolve(strict=True)
    home=Path(home).resolve()
    registry=json.loads((source/'profiles/accounts.json').read_text('utf-8-sig'))
    destination=home/'profiles'
    destination.mkdir(parents=True,exist_ok=True)
    target=destination/'accounts.json'
    current=json.loads(target.read_text('utf-8-sig')) if target.exists() else {'version':1,'accounts':{}}
    imported=0
    for key,entry in registry['accounts'].items():
        alias=entry.get('alias') or Path(key).stem
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',alias): raise ValueError('Invalid profile alias')
        profile=destination/alias
        profile.mkdir(exist_ok=True)
        source_profile=source/'profiles'/alias
        for name in ('token.txt','web-session.json'):
            f=source_profile/name
            if f.is_symlink(): raise ValueError('Profile files must not be symbolic links')
            if f.is_file(): shutil.copy2(f,profile/name)
        current['accounts'][key]={k:v for k,v in entry.items() if k in ('alias','username','browser','cookie_expires')}
        current['accounts'][key].update(alias=alias,token_file=f'profiles/{alias}/token.txt',profile_dir=f'profiles/{alias}',cookie_file=f'profiles/{alias}/web-session.json')
        imported+=1
    target.write_text(json.dumps(current,indent=2),encoding='utf-8')
    # Saved credentials remain private and are used only by the account-owned recovery module.
    credentials=source/'profiles/credentials.json'
    if credentials.is_file():
        existing=destination/'credentials.json'
        data=json.loads(existing.read_text('utf-8-sig')) if existing.exists() else {'version':1,'accounts':{}}
        incoming=json.loads(credentials.read_text('utf-8-sig'))
        data['accounts'].update({k:{n:v for n,v in row.items() if n in ('username','password')} for k,row in incoming.get('accounts',{}).items() if k in current['accounts']})
        existing.write_text(json.dumps(data),encoding='utf-8')
    return imported

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('source');parser.add_argument('home')
    args=parser.parse_args()
    print('Imported',import_profiles(args.source,args.home),'account profiles into OpenResearch private storage.')
