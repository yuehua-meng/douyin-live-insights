"""Linux/root: JSON credentials over stdin only. Never print credential values."""
import json
import argparse
import getpass
import re
import secrets
import subprocess
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interactive',action='store_true')
    args=parser.parse_args()
    data = ({'bot_id':input('Bot ID: ').strip(), 'secret':getpass.getpass('Secret: ').strip()}
            if args.interactive else json.load(sys.stdin))
    for key in ('bot_id','secret'):
        if not isinstance(data.get(key),str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,200}',data[key]):
            raise SystemExit('Invalid smart-bot credentials; configuration unchanged')
    target = Path('/etc/douyin-recorder/analysis/wecom.env')
    target.parent.mkdir(parents=True,exist_ok=True)
    nonce = secrets.token_hex(4).upper()
    lines = ['WECOM_TRANSPORT=bot','WECOM_DIGEST_ENABLED=1',
             'WECOM_BOT_ID='+data['bot_id'],'WECOM_BOT_SECRET='+data['secret'],
             'WECOM_BIND_CODE='+nonce]
    temporary = target.with_suffix('.env.tmp')
    temporary.touch(mode=0o600,exist_ok=True)
    temporary.chmod(0o600)
    temporary.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    temporary.replace(target)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','douyin-wecom-bot'],check=True)
    subprocess.run(['systemctl','restart','douyin-wecom-bot','douyin-analysis-api','douyin-analysis-scheduler'],check=True)
    print(json.dumps({'configured':True,'binding_command':'绑定日报 '+nonce},ensure_ascii=False))


if __name__=='__main__':main()
