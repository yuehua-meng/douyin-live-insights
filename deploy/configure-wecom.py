"""Run on Linux as root. Read the webhook from stdin, never print its secret."""
from pathlib import Path
import argparse
import getpass
import shlex
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.daily_digest import valid_webhook


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interactive',action='store_true')
    args=parser.parse_args()
    value = (getpass.getpass('WeCom group webhook: ') if args.interactive else sys.stdin.read()).strip()
    if not valid_webhook(value) or any(c.isspace() for c in value):
        raise SystemExit('Invalid enterprise WeChat group webhook; configuration unchanged')
    target = Path('/etc/douyin-recorder/analysis/wecom.env')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.env.tmp')
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.chmod(0o600)
    temporary.write_text('WECOM_TRANSPORT=webhook\nWECOM_WEBHOOK_URL='+shlex.quote(value)+'\n', encoding='utf-8')
    temporary.replace(target)
    schedule=target.with_name('automation.env')
    lines=schedule.read_text(encoding='utf-8').splitlines()
    lines=[line for line in lines if not line.startswith('WECOM_DIGEST_ENABLED=')]
    schedule.write_text('\n'.join(lines+['WECOM_DIGEST_ENABLED=1'])+'\n',encoding='utf-8')
    schedule.chmod(0o600)
    subprocess.run(['systemctl', 'stop', 'douyin-wecom-bot'], check=False, capture_output=True)
    subprocess.run(['systemctl', 'restart', 'douyin-analysis-api', 'douyin-analysis-scheduler'], check=True)
    print('Group webhook configured. Delivery follows the saved daily schedule; no test message sent.')


if __name__ == '__main__':
    main()
