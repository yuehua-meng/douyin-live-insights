#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
source /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
  echo "This installer targets Ubuntu 24.04. See docs/deployment.md." >&2; exit 1
fi
if ! id douyinrec >/dev/null 2>&1; then
  echo "Install the recorder first: sudo bash deploy/install-ubuntu.sh" >&2; exit 1
fi
source_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_dir=/opt/douyin-recorder
if [[ "$source_dir" == "$app_dir" ]]; then
  echo "Run this installer from a separate checkout, not /opt/douyin-recorder." >&2; exit 1
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv ffmpeg
if ! id douyinanalysis >/dev/null 2>&1; then
  useradd --system --home-dir /srv/douyin-recorder/analysis --shell /usr/sbin/nologin --groups douyinrec douyinanalysis
fi
install -d -m 0755 "$app_dir/analysis" "$app_dir/deploy"
cp -R "$source_dir/analysis/." "$app_dir/analysis/"
for file in recorder.py audio_quality.py media_metadata.py requirements-analysis.txt; do
  install -m 0644 "$source_dir/$file" "$app_dir/$file"
done
for file in configure-dashboard.py write-services.py download_models.py configure-wecom.py configure-wecom-bot.py; do
  install -m 0644 "$source_dir/deploy/$file" "$app_dir/deploy/$file"
done
install -m 0644 "$source_dir/deploy/douyin-wecom-bot.service" /etc/systemd/system/douyin-wecom-bot.service
install -d -o douyinanalysis -g douyinanalysis -m 0750 /srv/douyin-recorder/analysis
install -d -o douyinanalysis -g douyinanalysis -m 0750 /srv/douyin-recorder/analysis/models
install -d -m 0700 /etc/douyin-recorder/analysis
python3 -m venv "$app_dir/.analysis-venv"
"$app_dir/.analysis-venv/bin/python" -m pip install --disable-pip-version-check -r "$app_dir/requirements-analysis.txt"
python3 "$app_dir/deploy/write-services.py"
echo "Installed. Continue with configure-dashboard.py, model download and HTTPS setup in docs/deployment.md."
