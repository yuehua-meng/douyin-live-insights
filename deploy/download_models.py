"""Fetch only official ONNX runtime assets and record content hashes."""
import hashlib
import json
from pathlib import Path
from modelscope.hub.snapshot_download import snapshot_download

root = Path('/srv/douyin-recorder/analysis/models')
repos = {
    'asr': 'damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx',
    'vad': 'damo/speech_fsmn_vad_zh-cn-16k-common-onnx',
    'punc': 'damo/punc_ct-transformer_cn-en-common-vocab471067-large-onnx',
}
manifest = {}
for name, repo in repos.items():
    dest = root / name
    snapshot_download(repo, local_dir=str(dest), max_workers=2,
        allow_patterns=['model_quant.onnx', 'model.onnx' if name == 'vad' else 'model_quant.onnx',
                        '*.yaml', '*.mvn', '*.json', '*.txt', '*LICENSE*', 'README*'])
    expected = 'model_quant.onnx'
    if not (dest / expected).is_file():
        raise RuntimeError('Missing ' + name + '/' + expected)
    hashes = {}
    for path in dest.rglob('*'):
        if path.is_file() and not path.name.startswith('.'):
            h = hashlib.sha256()
            with path.open('rb') as file:
                for block in iter(lambda: file.read(1024*1024), b''):
                    h.update(block)
            hashes[str(path.relative_to(dest))] = h.hexdigest()
    manifest[name] = {'repository': repo, 'files': hashes}
    print('READY=' + name, flush=True)
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
