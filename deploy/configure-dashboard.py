"""Configure a fresh Linux installation; never echo passwords or API keys."""
import argparse
import getpass
import hashlib
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',required=True,help='Public HTTPS origin, e.g. https://live.example.com')
    args=parser.parse_args()
    url=urlsplit(args.origin)
    if (url.scheme!='https' or not url.hostname or url.username or url.password
            or url.path not in ('','/') or url.query or url.fragment
            or re.search(r'[\s\"\x27\\]',args.origin)):
        raise SystemExit('Use a plain HTTPS origin without a path, query or credentials.')
    origin=args.origin.rstrip('/')
    root=Path('/etc/douyin-recorder/analysis')
    if (root/'admin.env').exists():
        raise SystemExit('Configuration already exists. Edit the existing files deliberately; refusing to overwrite.')
    password=getpass.getpass('Web admin password (at least 12 characters): ')
    if len(password)<12 or password!=getpass.getpass('Repeat admin password: '):
        raise SystemExit('Password too short or confirmation differs.')
    key=getpass.getpass('DeepSeek API key (blank to configure later): ').strip()
    if key and not re.fullmatch(r'[A-Za-z0-9_-]+',key):
        raise SystemExit('Invalid API key format.')
    salt=secrets.token_hex(16)
    hashed=salt+':'+hashlib.scrypt(password.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
    os.umask(0o077)
    root.mkdir(parents=True,exist_ok=True)
    values={
        'admin.env':'ADMIN_PASSWORD_HASH='+hashed+'\n',
        'deepseek.env':'DEEPSEEK_API_KEY='+key+'\n',
        'common.env':f'ADMIN_ORIGIN={origin}\nDEEPSEEK_MODEL=deepseek-flash\n'
            'DAILY_BUDGET_MICRO_USD=1000000\nTASK_BUDGET_MICRO_USD=200000\n'
            'INPUT_USD_PER_MILLION=0.3\nOUTPUT_USD_PER_MILLION=1.2\n',
        'automation.env':'DAILY_BATCH_MODE=1\nDAILY_PROCESS_HOUR=9\nDAILY_SEND_HOUR=11\nWECOM_DIGEST_ENABLED=0\n',
    }
    for name,value in values.items():
        path=root/name
        if path.exists():raise SystemExit('Refusing to overwrite existing '+name)
    for name,value in values.items():
        path=root/name;path.write_text(value,encoding='utf-8');path.chmod(0o600)
    print('Configuration written. No services started and no messages sent.')


if __name__=='__main__':main()
