"""Persist small duration records beside finalized audio; never probe on HTTP reads."""
import json
import math
import os
from pathlib import Path
import subprocess
import uuid


def duration_path(path):
    return path.with_name(path.name+'.duration.json')


def cached_duration_ms(path):
    try:
        stat=path.stat()
        data=json.loads(duration_path(path).read_text(encoding='utf-8'))
        duration=data.get('duration_ms')
        if (data.get('size'),data.get('mtime_ns'))!=(stat.st_size,stat.st_mtime_ns):
            return None
        if isinstance(duration,int) and not isinstance(duration,bool) and duration>0:
            return duration
    except (OSError,ValueError,TypeError):
        pass
    return None


def cache_duration(path, ffprobe='ffprobe'):
    existing=cached_duration_ms(path)
    if existing is not None:return existing
    try:
        before=path.stat()
        result=subprocess.run([ffprobe,'-v','error','-select_streams','a:0',
            '-show_entries','format=duration:stream=duration','-of','json',str(path)],
            capture_output=True,text=True,timeout=10,check=False)
        if result.returncode:return None
        data=json.loads(result.stdout)
        values=[data.get('format',{}).get('duration')]+[s.get('duration') for s in data.get('streams',[])]
        duration=next((float(v) for v in values if v not in (None,'N/A') and math.isfinite(float(v)) and float(v)>0),None)
        if duration is None:return None
        after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return None
        ms=max(1,round(duration*1000))
        metadata={'duration_ms':ms,'size':after.st_size,'mtime_ns':after.st_mtime_ns,'source':'ffprobe'}
        dest=duration_path(path);temp=dest.with_name(dest.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temp.write_text(json.dumps(metadata),encoding='utf-8')
            os.replace(temp,dest)
        finally:
            temp.unlink(missing_ok=True)
        return ms
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):
        return None
