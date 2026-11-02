import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

from .catalog import safe_file
from .store import digest


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def model_fingerprint(settings):
    manifest = Path(settings.asr_model).parent / 'manifest.json'
    return settings.model_version + ':' + (file_hash(manifest) if manifest.is_file() else 'unverified')


class Engine:
    def __init__(self, settings):
        from funasr_onnx import Paraformer, Fsmn_vad, CT_Transformer
        for directory, filename in [(settings.asr_model, 'model_quant.onnx'),
                                    (settings.vad_model, 'model_quant.onnx'),
                                    (settings.punc_model, 'model_quant.onnx')]:
            if not (Path(directory) / filename).is_file():
                raise RuntimeError('ASR 模型尚未安装完整')
        opts = dict(device_id='-1', intra_op_num_threads=settings.threads)
        self.asr = Paraformer(settings.asr_model, quantize=True, batch_size=1, **opts)
        self.vad = Fsmn_vad(settings.vad_model, quantize=True, **opts)
        self.punc = CT_Transformer(settings.punc_model, quantize=True, **opts)

    def recognize(self, samples):
        import numpy as np
        if not len(samples):
            return []
        duration_ms = len(samples) / 16
        # Container durations can leave a few milliseconds after the last 60s
        # window. FunASR's fbank/LFR indexes its first frame even when a waveform
        # is shorter than one frame. Give VAD enough context with zero padding;
        # always clip its intervals to the ORIGINAL samples, never the padding.
        vad_samples = np.pad(samples, (0, 16000-len(samples))) if len(samples) < 16000 else samples
        intervals = self.vad(vad_samples)
        intervals = intervals[0] if intervals else []
        output = []
        for start, end in intervals:
            start, end = max(0, int(start)), min(int(end), int(duration_ms))
            if end <= start:
                continue
            for begin in range(start, end, 25_000):
                finish = min(end, begin + 25_000)
                if finish-begin < 150:
                    continue
                predictions = self.asr(samples[begin*16:finish*16])
                if not predictions:
                    continue
                raw = predictions[0].get('preds', '')
                if isinstance(raw, (tuple, list)):
                    raw = raw[0] if raw else ''
                if not isinstance(raw, str) or not raw.strip():
                    continue
                punctuated = self.punc(raw)
                text = punctuated[0] if isinstance(punctuated, (list, tuple)) else punctuated
                output.append(dict(start_ms=begin, end_ms=finish, text=str(text).strip(),
                                   raw_text=raw, timing='vad_interval'))
        return output


def transcribe(recording, settings, store, engine_loader, check, progress):
    import numpy as np
    path = safe_file(settings.recordings, recording)
    if shutil.disk_usage(settings.data).free < settings.min_free_bytes:
        raise RuntimeError('磁盘空间不足，已暂停分析以保护录音')
    fingerprint = file_hash(path)
    version = model_fingerprint(settings)
    key = digest([fingerprint, version, 'windows60-v1'])
    cached = store.cached('transcripts', key)
    if cached:
        # Same bytes can have different filenames; references belong to this selection.
        if cached['recording']['id'] == recording['id']:
            return cached
        cached = dict(cached, id=digest([key, recording['id']]), recording=recording)
        for i, seg in enumerate(cached['segments']):
            seg['id'] = f"{cached['id']}:{i}"
        store.save_transcript(cached)
        return cached
    probe = subprocess.run([settings.ffprobe, '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', str(path)],
        capture_output=True, timeout=30, check=False)
    if probe.returncode:
        raise ValueError('录音损坏或无法解码')
    meta = json.loads(probe.stdout)
    duration = float(meta.get('format', {}).get('duration', 0))
    if not meta.get('streams') or not math.isfinite(duration) or not 0.1 <= duration <= 21600:
        raise ValueError('音轨或时长无效（单文件上限 6 小时）')
    segments = []
    decode_notes = []
    windows = math.ceil(duration / 60)
    for index in range(windows):
        check()
        block_key = digest([key, index])
        block = store.cached('checkpoints', block_key)
        if block is None:
            decoded = subprocess.run([settings.ffmpeg, '-hide_banner', '-loglevel', 'error',
                '-nostdin', '-ss', str(index*60), '-i', str(path), '-t', '60',
                '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', 'pipe:1'],
                capture_output=True, timeout=90, check=False)
            remaining = duration - index*60
            rounding_tail = (decoded.returncode == 0 and not decoded.stdout
                             and index > 0 and index == windows-1
                             and 0 < remaining <= 0.1 + 1e-9)
            if decoded.returncode or (not decoded.stdout and not rounding_tail):
                raise ValueError('音频片段解码失败')
            if len(decoded.stdout) > 4_000_000:
                raise ValueError('解码片段超出资源上限')
            if rounding_tail:
                # M4A's container clock may extend just past a whole minute
                # even though FFmpeg successfully decoded every audio sample.
                # Cache the explanation with this empty final block so a later
                # resume preserves it and never reruns earlier ASR windows.
                block = dict(segments=[], decode_note=dict(
                    kind='container_rounding_tail', window=index+1,
                    start_ms=index*60000, duration_ms=round(remaining*1000),
                    message='容器时长存在不超过 100 毫秒的尾差；末尾解码成功但无音频样本，已保留前面的完整转写。'))
            else:
                samples = np.frombuffer(decoded.stdout, dtype='<f4').copy()
                block = engine_loader().recognize(samples)
            check()
            store.cache('checkpoints', block_key, block)
        if isinstance(block, dict):
            decode_notes.append(block['decode_note'])
            block = block['segments']
        for seg in block:
            segments.append(dict(seg, start_ms=seg['start_ms']+index*60000,
                                 end_ms=min(round(duration*1000), seg['end_ms']+index*60000)))
        progress(index+1, windows)
    check()
    safe_file(settings.recordings, recording)
    transcript_id = digest([key, recording['id']])
    for i, seg in enumerate(segments):
        seg['id'] = f'{transcript_id}:{i}'
    body = dict(id=transcript_id, recording=recording, duration_ms=round(duration*1000),
                audio_sha256=fingerprint, model_version=version, segments=segments,
                timing='VAD 讲话区间，非逐字对齐；窗口边界可能切断词语',
                status='ready' if segments else 'no_speech', revision=1,
                decode_notes=decode_notes)
    store.save_transcript(body, key)
    return body


def timecode(ms, srt=False):
    hours, rest = divmod(max(0, round(ms)), 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02}{"," if srt else "."}{millis:03}'


def export_transcript(body, kind):
    if kind == 'json':
        return json.dumps(body, ensure_ascii=False, indent=2)
    if kind == 'srt':
        return '\n\n'.join(f"{i+1}\n{timecode(s['start_ms'], True)} --> {timecode(s['end_ms'], True)}\n{s['text']}"
                           for i, s in enumerate(body['segments']))
    return '\n'.join(f"[{timecode(s['start_ms'])}–{timecode(s['end_ms'])}] {s['text']}" for s in body['segments'])
