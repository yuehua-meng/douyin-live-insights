#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-ubuntu.sh" >&2
  exit 1
fi
if [[ ! -f /etc/os-release ]]; then
  echo "Cannot identify this Linux distribution" >&2
  exit 1
fi
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  echo "This installer targets Ubuntu 24.04; review installation steps for ${PRETTY_NAME:-this system}" >&2
  exit 1
fi

source_dir="$(cd "$(dirname "$0")/.." && pwd)"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ffmpeg

if ! id douyinrec >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin douyinrec
fi
install -d -m 0755 /opt/douyin-recorder /etc/douyin-recorder
install -d -o douyinrec -g douyinrec -m 0750 /srv/douyin-recorder
install -d -o douyinrec -g douyinrec -m 0750 /srv/douyin-recorder/work /srv/douyin-recorder/recordings
install -m 0644 "$source_dir/recorder.py" /opt/douyin-recorder/recorder.py
install -m 0644 "$source_dir/audio_quality.py" /opt/douyin-recorder/audio_quality.py
install -m 0644 "$source_dir/media_metadata.py" /opt/douyin-recorder/media_metadata.py
install -m 0644 "$source_dir/requirements.txt" /opt/douyin-recorder/requirements.txt
if [[ ! -f /etc/douyin-recorder/config.toml ]]; then
  install -o douyinrec -g douyinrec -m 0640 "$source_dir/config.example.toml" /etc/douyin-recorder/config.toml
fi
python3 -m venv /opt/douyin-recorder/.venv
/opt/douyin-recorder/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/douyin-recorder/requirements.txt
install -m 0644 "$source_dir/deploy/douyin-recorder.service" /etc/systemd/system/douyin-recorder.service
systemctl daemon-reload
systemctl enable douyin-recorder.service

echo "Installed. Review /etc/douyin-recorder/config.toml and run a foreground --once --max-seconds 15 test before starting the service."
