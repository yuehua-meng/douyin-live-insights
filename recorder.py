#!/usr/bin/env python3
"""Single-room Douyin audio recorder for Linux; also supports local source testing."""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from audio_quality import check_entry, filtered, quarantine_unfinalized, quarantine_root
from media_metadata import cache_duration


LOG = logging.getLogger("douyin-recorder")
STOP = False
LOCAL_ZONE = timezone(timedelta(hours=8))


class FFmpegProgress:
    """Track media progress even while the muxer's file buffer hasn't flushed."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last_us = None
        self.last_tick = 0.0

    def feed(self, line: str):
        if not line.startswith('out_time_us='):
            return
        try:
            value = int(line.strip().split('=', 1)[1])
        except ValueError:
            return
        if value < 0:
            return
        with self.lock:
            if value != self.last_us:
                self.last_us = value
                self.last_tick = time.monotonic()

    def read(self, stream):
        for line in stream:
            self.feed(line)

    def tick(self):
        with self.lock:
            return self.last_tick


def now() -> str:
    return datetime.now(LOCAL_ZONE).isoformat(timespec="seconds")


def stop_signal(_sig, _frame) -> None:
    global STOP
    STOP = True


def redact(message: str) -> str:
    message = re.sub(r"https?://[^\s'\"]+", "[URL redacted]", message)
    return re.sub(r"(?i)(cookie|token|sessionid)\s*[=:]\s*[^\s,;]+", r"\1=[redacted]", message)


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def pause(seconds: int) -> None:
    end = time.monotonic() + seconds
    while not STOP and time.monotonic() < end:
        time.sleep(min(1, end - time.monotonic()))


def monitor_status(cfg, state, offline_since=None):
    """Observed resolver state, not a claim about an official broadcast session."""
    try:
        cfg.recordings_dir.mkdir(parents=True,exist_ok=True)
        atomic_json(cfg.recordings_dir/'monitor.json',dict(douyin_id=cfg.douyin_id,
                    live_state=state,observed_at=time.time(),offline_since=offline_since))
    except OSError:
        LOG.warning('Cannot update recording monitor status')


@dataclass(frozen=True)
class Config:
    douyin_id: str
    room_url: str
    recordings_dir: Path
    work_dir: Path
    poll_seconds: int
    segment_seconds: int
    min_free_gb: float
    ffmpeg: str

    @classmethod
    def read(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        target = data["target"]
        runtime = data["runtime"]
        paths = data["paths"]
        room_url = target.get("room_url", "").strip()
        if room_url:
            parsed = urlparse(room_url)
            if parsed.scheme != "https" or parsed.hostname not in {"live.douyin.com", "douyin.com"}:
                raise ValueError("room_url must be an https://live.douyin.com/... URL")
        ffmpeg = runtime.get("ffmpeg", "ffmpeg")
        if ffmpeg == "bundled":
            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError as exc:
                raise RuntimeError("Install requirements-local.txt for bundled FFmpeg") from exc
        return cls(
            douyin_id=target["douyin_id"], room_url=room_url,
            recordings_dir=Path(paths["recordings_dir"]).expanduser(),
            work_dir=Path(paths["work_dir"]).expanduser(),
            poll_seconds=max(10, int(runtime.get("poll_seconds", 60))),
            segment_seconds=max(2, int(runtime.get("segment_seconds", 1800))),
            min_free_gb=float(runtime.get("min_free_gb", 5)),
            ffmpeg=ffmpeg,
        )


def run_command(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


class ResolverError(Exception):
    pass


class Offline(ResolverError):
    pass


def resolve_stream(cfg: Config) -> str:
    if not cfg.room_url:
        raise ResolverError("room_url is not set; confirm the target room URL first")
    result = run_command(
        [sys.executable, "-m", "streamlink", "--no-config", "--stream-url", cfg.room_url, "worst"], timeout=35
    )
    if result.returncode != 0:
        diagnostic = (result.stderr + '\n' + result.stdout).strip()
        err = diagnostic.lower()
        if "no playable streams found" in err or "no streams found" in err or "offline" in err:
            raise Offline("room is offline")
        raise ResolverError(redact(diagnostic[-400:] or "streamlink failed"))
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if urlparse(url).scheme not in {"https", "http"}:
        raise ResolverError("streamlink did not return a playable HTTP URL")
    return url


def probe_audio(cfg: Config, source: str) -> str:
    result = run_command(
        [cfg.ffmpeg, "-hide_banner", "-rw_timeout", "15000000", "-i", source], timeout=35
    )
    match = re.search(r"Stream #\d+:\d+[^\n]*Audio:\s*([A-Za-z0-9_]+)", result.stderr)
    if match:
        return match.group(1).lower()
    raise ResolverError("stream has no audio track")


def check_space(cfg: Config) -> None:
    cfg.recordings_dir.mkdir(parents=True, exist_ok=True)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(cfg.recordings_dir).free / 1_000_000_000
    if free_gb < cfg.min_free_gb:
        raise RuntimeError(f"disk space low: {free_gb:.1f} GB free")


def media_ok(cfg: Config, source: Path) -> bool:
    result = run_command(
        [cfg.ffmpeg, "-v", "error", "-i", str(source), "-map", "0:a:0",
         "-f", "null", "-"], timeout=180
    )
    return result.returncode == 0 and source.exists() and source.stat().st_size > 1024


def finalize_segment(cfg: Config, source: Path, dest_dir: Path, session: dict) -> bool:
    index = int(source.stem.split("_")[-1])
    target = dest_dir / f"part_{index:04d}.m4a"
    archived=quarantine_root(cfg.recordings_dir)/target.relative_to(cfg.recordings_dir)
    if target.exists() or archived.exists():
        entry=next((f for f in session['files'] if f['name']==target.name),None)
        if entry is None:
            entry={'name':target.name,'bytes':target.stat().st_size if target.exists() else archived.stat().st_size}
            session['files'].append(entry)
        check_entry(dest_dir/'session.json',session,entry,cfg.recordings_dir,cfg.ffmpeg)
        if not target.exists() and not filtered(entry):
            LOG.error('Quarantine recovery not verified; raw segment kept')
            return False
        if target.exists():cache_duration(target)
        atomic_json(dest_dir/'session.json',session)
        source.unlink(missing_ok=True)
        return True
    temp = target.with_suffix(".pending")
    result = run_command(
        [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", str(source),
         "-map", "0:a:0", "-c:a", "copy", "-f", "mp4", str(temp)], timeout=180
    )
    if result.returncode:
        temp.unlink(missing_ok=True)
        quarantine_unfinalized(source, cfg.recordings_dir, session)
        atomic_json(dest_dir / 'session.json', session)
        LOG.warning("Segment %s isolated: remux_failed; raw source preserved", source.name)
        return True
    os.replace(temp, target)
    if not any(x["name"] == target.name for x in session["files"]):
        session["files"].append({"name": target.name, "bytes": target.stat().st_size})
        check_entry(dest_dir / 'session.json', session, session['files'][-1], cfg.recordings_dir, cfg.ffmpeg)
        if target.exists():cache_duration(target)
        atomic_json(dest_dir / "session.json", session)
    source.unlink(missing_ok=True)
    LOG.info("Processed %s (filtered=%s)", target.name,
             any(x['name']==target.name and filtered(x) for x in session['files']))
    return True


def recover_orphans(cfg: Config) -> None:
    if not cfg.work_dir.exists():
        return
    for work_dir in cfg.work_dir.iterdir():
        if not work_dir.is_dir() or not list(work_dir.glob("part_*.ts")):
            continue
        matches = list((cfg.recordings_dir / cfg.douyin_id).glob(f"*/*_{work_dir.name}"))
        if len(matches) != 1:
            LOG.error("Cannot identify session for unfinished work %s", work_dir.name)
            continue
        dest_dir = matches[0]
        manifest = dest_dir / "session.json"
        try:
            session = json.loads(manifest.read_text(encoding="utf-8"))
            for segment in sorted(work_dir.glob("part_*.ts")):
                finalize_segment(cfg, segment, dest_dir, session)
            session["status"] = "interrupted"
            session["ended_at"] = session.get("ended_at") or now()
            atomic_json(manifest, session)
        except (OSError, ValueError) as exc:
            LOG.error("Recovery failed for %s: %s", work_dir.name, redact(str(exc)))
        if not any(work_dir.glob("part_*.ts")):
            (work_dir / "ffmpeg.log").unlink(missing_ok=True)
            if not any(work_dir.iterdir()):
                work_dir.rmdir()


def recording_session(cfg: Config, source: str, *, max_seconds: int = 0) -> None:
    check_space(cfg)
    session_id = uuid.uuid4().hex[:10]
    started = datetime.now(LOCAL_ZONE)
    name = started.strftime("%H%M%S") + "_" + session_id
    dest_dir = cfg.recordings_dir / cfg.douyin_id / started.strftime("%Y-%m-%d") / name
    work_dir = cfg.work_dir / session_id
    dest_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    session = {"session_id": session_id, "douyin_id": cfg.douyin_id,
               "started_at": now(), "ended_at": None, "status": "recording", "files": [], "gaps": []}
    atomic_json(dest_dir / "session.json", session)
    codec = probe_audio(cfg, source)
    encoding = ["-c:a", "copy"] if codec == "aac" else ["-c:a", "aac", "-b:a", "128k"]
    command = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-rw_timeout", "15000000"] if source.startswith(("http://", "https://")) else [
               cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    command += ["-i", source, "-map", "0:a:0", "-vn", *encoding,
                "-progress", "pipe:1", "-stats_period", "2"]
    if max_seconds:
        command += ["-t", str(max_seconds)]
    command += [
                "-f", "segment", "-segment_time", str(cfg.segment_seconds),
                "-segment_format", "mpegts", "-reset_timestamps", "1",
                str(work_dir / "part_%04d.ts")]
    ffmpeg_log = (work_dir / "ffmpeg.log").open("w+", encoding="utf-8")
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=ffmpeg_log, text=True, encoding='utf-8', errors='replace')
    media_progress = FFmpegProgress()
    progress_thread = threading.Thread(target=media_progress.read, args=(proc.stdout,), daemon=True)
    progress_thread.start()
    LOG.info("Recording started: %s", session_id)
    recording_started = time.monotonic()
    last_progress = time.monotonic()
    sizes: dict[Path, int] = {}
    reason = "stream_ended"
    last_monitor=0
    try:
        while proc.poll() is None:
            if time.monotonic()-last_monitor>=15:
                monitor_status(cfg,'recording')
                last_monitor=time.monotonic()
            if STOP:
                reason = "manual_stop"
                proc.terminate()
                break
            if max_seconds and time.monotonic() - recording_started >= max_seconds + 10:
                reason = "test_limit"
                proc.terminate()
                break
            check_space(cfg)
            files = sorted(work_dir.glob("part_*.ts"))
            for completed in files[:-1]:
                finalize_segment(cfg, completed, dest_dir, session)
            files = sorted(work_dir.glob("part_*.ts"))
            if files:
                newest = files[-1]
                size = newest.stat().st_size
                if size > sizes.get(newest, -1) or len(files) > len(sizes):
                    last_progress = time.monotonic()
                sizes = {p: p.stat().st_size for p in files}
            last_progress = max(last_progress, media_progress.tick())
            if time.monotonic() - last_progress > 45:
                reason = "stalled"
                proc.terminate()
                break
            time.sleep(2)
    except RuntimeError:
        reason = "disk_full"
        proc.terminate()
        raise
    finally:
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        progress_thread.join(timeout=2)
        proc.stdout.close()
        for segment in sorted(work_dir.glob("part_*.ts")):
            finalize_segment(cfg, segment, dest_dir, session)
        ffmpeg_log.flush()
        ffmpeg_log.seek(0)
        error = ffmpeg_log.read()[-500:]
        ffmpeg_log.close()
        if error:
            LOG.warning("FFmpeg: %s", redact(error))
        session["ended_at"] = now()
        if max_seconds and reason == "stream_ended":
            reason = "test_limit"
        if any(work_dir.glob("part_*.ts")):
            session["gaps"].append({"at": now(), "reason": "unfinished_segment"})
        session["status"] = reason if any(not filtered(f) for f in session["files"]) else "no_valid_audio"
        atomic_json(dest_dir / "session.json", session)
        (work_dir / "ffmpeg.log").unlink(missing_ok=True)
        if not any(work_dir.iterdir()):
            work_dir.rmdir()
        LOG.info("Recording ended: %s (%s)", session_id, session["status"])


def serve(cfg: Config, *, once: bool = False, test_source: str = "", max_seconds: int = 0) -> None:
    for binary in (cfg.ffmpeg,):
        if not shutil.which(binary):
            raise RuntimeError(f"Required executable not found: {binary}")
    if not test_source and importlib.util.find_spec("streamlink") is None:
        raise RuntimeError("Required Python module not found: streamlink")
    recover_orphans(cfg)
    errors = 0
    offline_since=None
    while not STOP:
        try:
            source = test_source or resolve_stream(cfg)
            errors = 0
            offline_since=None
            monitor_status(cfg,'recording')
            recording_session(cfg, source, max_seconds=max_seconds)
            if once or STOP:
                return
        except Offline:
            offline_since=offline_since or time.time()
            monitor_status(cfg,'offline',offline_since)
            LOG.info("No playable stream (offline or unavailable); checking again in %ss", cfg.poll_seconds)
            if once:
                return
            pause(cfg.poll_seconds)
        except (ResolverError, RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            offline_since=None
            monitor_status(cfg,'unknown')
            errors += 1
            LOG.error("Task needs attention: %s", redact(str(exc)))
            if once:
                raise
            pause(min(300, [5, 15, 30, 60, 120][min(errors - 1, 4)]))
        else:
            pause(cfg.poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--once", action="store_true", help="stop after one recording attempt")
    parser.add_argument("--test-source", help="local media file or test stream, skips Douyin lookup")
    parser.add_argument("--max-seconds", type=int, default=0, help="limit one recording for verification")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    try:
        serve(Config.read(args.config), once=args.once, test_source=args.test_source or "", max_seconds=args.max_seconds)
        return 0
    except (KeyError, ValueError, RuntimeError, FileNotFoundError) as exc:
        LOG.error("Startup failed: %s", redact(str(exc)))
        return 2


if __name__ == "__main__":
    sys.exit(main())
