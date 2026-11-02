"""Conservative, reversible audio filtering shared by recorder and archive sweep."""
import hashlib
import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

POLICY='audio-quality-v1'
SILENCE_PEAK_DB=-80.0


def filtered(item):
    return item.get('quality', {}).get('decision')=='quarantine'


def inspect_audio(path, ffmpeg='ffmpeg'):
    base={'policy':POLICY,'checked_at':datetime.now(timezone.utc).isoformat()}
    if path.stat().st_size==0:
        return dict(base,decision='quarantine',reason='empty_file')
    try:
        r=subprocess.run([ffmpeg,'-hide_banner','-nostdin','-v','info','-xerror',
            '-threads','1','-i',str(path),'-map','0:a:0','-vn','-af','volumedetect',
            '-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,
            encoding='utf-8',errors='replace',timeout=180)
    except (OSError,subprocess.TimeoutExpired):
        return dict(base,decision='review',reason='inspection_unavailable')
    if r.returncode:
        return dict(base,decision='quarantine',reason='decode_error_or_no_audio')
    samples=re.findall(r'n_samples:\s*(\d+)',r.stderr)
    peaks=re.findall(r'max_volume:\s*(-?[\d.]+|-inf)\s*dB',r.stderr)
    if not samples or not peaks:
        return dict(base,decision='review',reason='missing_measurement')
    count=int(samples[-1]);peak=float(peaks[-1])
    if not math.isfinite(peak):
        peak=-120.0
    base.update(samples=count,peak_db=peak)
    if count==0:
        return dict(base,decision='quarantine',reason='no_decoded_samples')
    if peak<=SILENCE_PEAK_DB:
        return dict(base,decision='quarantine',reason='near_digital_silence')
    return dict(base,decision='keep',reason='audible_audio')


def atomic_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temp,path)


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def quarantine_root(recordings):
    return recordings.resolve().parent/'quarantine'


def prepare_quarantine_folder(folder, recordings):
    root=quarantine_root(recordings)
    folder.mkdir(parents=True,exist_ok=True)
    # A root-run historical sweep must not leave future recorder directories unwritable.
    if hasattr(os,'geteuid') and os.geteuid()==0:
        owner=recordings.stat()
        current=folder
        while current==root or current.is_relative_to(root):
            os.chown(current,owner.st_uid,owner.st_gid)
            current.chmod(0o750)
            if current==root:break
            current=current.parent


def quarantine_file(path, recordings, quality):
    root=recordings.resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Unsafe audio path')
    relative=path.relative_to(root)
    dest=quarantine_root(root)/relative
    if not dest.resolve().is_relative_to(quarantine_root(root)):
        raise ValueError('Unsafe quarantine path')
    prepare_quarantine_folder(dest.parent,root)
    checksum=sha256(path)
    if dest.exists():
        raise ValueError('Quarantine destination already exists; refusing overwrite')
    atomic_json(dest.with_name(dest.name+'.audit.json'),dict(original=relative.as_posix(),
                sha256=checksum,bytes=path.stat().st_size,quality=quality))
    os.replace(path,dest)
    return quality


def check_entry(manifest, session, item, recordings, ffmpeg='ffmpeg'):
    root=recordings.resolve();path=manifest.parent/item['name']
    if not re.fullmatch(r'part_\d{4,}\.m4a',item['name']):
        return False
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        return False
    previous=item.get('quality',{})
    if filtered(item) or previous.get('reason')=='manual_restore':
        return False
    # Recover a move completed before the manifest update.
    if not path.is_file():
        audit=quarantine_root(root)/path.relative_to(root)
        report=audit.with_name(audit.name+'.audit.json')
        if audit.is_file() and report.is_file():
            saved=json.loads(report.read_text(encoding='utf-8'))
            if sha256(audit)!=saved['sha256'] or saved['quality']['decision']!='quarantine':
                return False
            item['quality']=saved['quality']
            return True
        return False
    stat=path.stat()
    if previous.get('policy')==POLICY and previous.get('decision')=='keep' and previous.get('size')==stat.st_size and previous.get('mtime_ns')==stat.st_mtime_ns:
        return False
    quality=inspect_audio(path,ffmpeg)
    quality.update(size=stat.st_size,mtime_ns=stat.st_mtime_ns)
    if quality['decision']=='quarantine':
        quarantine_file(path,root,quality)
    item['quality']=quality
    return True


def counts(recordings):
    result={'quarantined':0,'kept':0,'review':0}
    for manifest in recordings.glob('*/*/*/session.json'):
        try:
            data=json.loads(manifest.read_text(encoding='utf-8'))
            result['quarantined']+=len(data.get('filtered_raw',[]))
            for item in data.get('files',[]):
                decision=item.get('quality',{}).get('decision')
                if decision=='quarantine':result['quarantined']+=1
                elif decision=='keep':result['kept']+=1
                elif decision=='review':result['review']+=1
        except (OSError,ValueError,TypeError):
            continue
    return result


def quarantine_unfinalized(source, recordings, session):
    dest=quarantine_root(recordings)/'unfinalized'/session['session_id']/source.name
    prepare_quarantine_folder(dest.parent,recordings)
    if dest.exists():raise ValueError('Unfinalized quarantine destination exists')
    quality={'policy':POLICY,'decision':'quarantine','reason':'remux_failed'}
    atomic_json(dest.with_name(dest.name+'.audit.json'),dict(original_work_path=str(source),
                sha256=sha256(source),bytes=source.stat().st_size,quality=quality))
    os.replace(source,dest)
    session.setdefault('filtered_raw',[]).append({'name':source.name,'quality':quality})
