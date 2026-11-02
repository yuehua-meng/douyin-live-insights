"""Install service definitions only. Starting them is a separate explicit step."""
from pathlib import Path
import subprocess

COMMON='''[Unit]
Description=Douyin Live Insights {name}
After=network-online.target
Wants=network-online.target

[Service]
User=douyinanalysis
Group=douyinanalysis
SupplementaryGroups=douyinrec
WorkingDirectory=/opt/douyin-recorder
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=OMP_NUM_THREADS=2
Environment=OPENBLAS_NUM_THREADS=2
Environment=NUMBA_CACHE_DIR=/srv/douyin-recorder/analysis/numba-cache
EnvironmentFile=/etc/douyin-recorder/analysis/common.env
EnvironmentFile=/etc/douyin-recorder/analysis/automation.env
EnvironmentFile=-/etc/douyin-recorder/analysis/wecom.env
{extra}
ExecStart=/opt/douyin-recorder/.analysis-venv/bin/python {command}
Restart=on-failure
RestartSec=10
TimeoutStopSec=40
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/srv/douyin-recorder/analysis

[Install]
WantedBy=multi-user.target
'''


def main():
    services={
        'api':('-m uvicorn analysis.api:create_app --factory --host 127.0.0.1 --port 8790 --no-access-log',
               'EnvironmentFile=/etc/douyin-recorder/analysis/admin.env'),
        'asr':('-m analysis.worker transcribing',''),
        'summary':('-m analysis.worker summarizing','EnvironmentFile=/etc/douyin-recorder/analysis/deepseek.env'),
        'scheduler':('-m analysis.automation',''),
    }
    for name,(command,extra) in services.items():
        Path(f'/etc/systemd/system/douyin-analysis-{name}.service').write_text(
            COMMON.format(name=name,command=command,extra=extra),encoding='utf-8')
    subprocess.run(['systemctl','daemon-reload'],check=True)
    print('API, ASR, summary and scheduler units installed; not started.')


if __name__=='__main__':main()
