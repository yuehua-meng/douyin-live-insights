import hashlib
import json
import re
from audio_quality import filtered
from media_metadata import cached_duration_ms


def scan(root, include_duration=False):
    root = root.resolve()
    result = []
    for manifest in sorted(root.glob('*/*/*/session.json'), reverse=True):
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(root):
            continue
        try:
            session = json.loads(manifest.read_text(encoding='utf-8'))
            for item in session.get('files', []):
                if filtered(item):
                    continue
                name = item.get('name', '')
                if not re.fullmatch(r'part_\d{4,}\.m4a', name):
                    continue
                path = manifest.parent / name
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                relative = path.relative_to(root).as_posix()
                stat = path.stat()
                row=dict(id=hashlib.sha256(relative.encode()).hexdigest()[:32],
                    path=relative, name=name, session=manifest.parent.relative_to(root).as_posix(),
                    started_at=session.get('started_at', ''), session_status=session.get('status', ''),
                    size=stat.st_size, mtime_ns=stat.st_mtime_ns)
                if include_duration:
                    row['duration_ms']=cached_duration_ms(path)
                result.append(row)
        except (OSError, ValueError, TypeError):
            continue
    return result


def safe_file(root, recording):
    root = root.resolve()
    path = root / recording['path']
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError('录音文件不可用')
    stat = path.stat()
    if stat.st_size != recording['size'] or stat.st_mtime_ns != recording['mtime_ns']:
        raise ValueError('录音文件已发生变化，请重新创建任务')
    return path
