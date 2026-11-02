"""Conservative transcript-content screening; no audio-quality guesses or model calls.

This is a gate before paid summarization, not an estimate of ASR confidence.
Only an explicitly completed transcript whose ENTIRE content is empty, filler,
or exact unintelligible/noise placeholders can be excluded. A short meaningful
answer, technical vocabulary, or insufficient business detail is still content.
"""
from collections import Counter
import re
import unicodedata


POLICY = 'transcript-content-v1'
FILLERS = frozenset('嗯啊呃唉哦噢欸诶')
PLACEHOLDERS = frozenset({
    '听不清', '听不清楚', '无法听清', '无法辨认', '无法识别', '不可辨认',
    '语音不清', '噪音', '噪声', '杂音', '背景噪音', '背景噪声',
    '静音', '无声', '无声音', '无语音', '无人声',
    'inaudible', 'unintelligible', 'noise', 'silence',
})
_ANNOTATION = re.compile(r'[\[【(（<](.*?)[\]】)）>]', re.DOTALL)
_SEPARATORS = re.compile(r'[\s，。！？、；：,.!?;:…]+')
REASONS = {
    'no_recognized_text': '转写已完成，但未识别到有效文字。',
    'filler_only': '全部转写仅包含语气词，没有可整理的实质内容。',
    'unintelligible_only': '全部转写仅有听不清或噪音占位标记。',
    'filler_and_unintelligible_only': '全部转写仅有语气词和听不清或噪音标记。',
    'meaningful_text': '保留有效文字，不按篇幅、时长或业务价值过滤。',
    'repetitive_text': '转写存在大量相同句段，保留内容供核对，不自动删除。',
    'transcript_incomplete': '转写尚未明确完成，不能据此过滤录音。',
    'invalid_transcript': '转写结构异常，不能据此过滤录音。',
}


def _plain(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    # Remove punctuation/spacing, never digits, Latin letters or real words.
    return ''.join(c for c in value if not c.isspace()
                   and not unicodedata.category(c).startswith('P'))


def _parts(value):
    """Return (text, explicit_placeholder) pairs without guessing from words.

    "静音" and "听不清" may be genuine short spoken commands/statements.
    Only exact bracketed annotations can be treated as non-speech markers.
    """
    value = unicodedata.normalize('NFKC', value)
    parts = []

    def literal(text):
        parts.extend((part, False) for token in _SEPARATORS.split(text)
                     if (part := _plain(token)))

    cursor = 0
    for match in _ANNOTATION.finditer(value):
        literal(value[cursor:match.start()])
        body = _plain(match.group(1))
        if body in PLACEHOLDERS:
            parts.append((body, True))
        else:
            # Unknown bracketed content remains real content, e.g. [客户需求].
            literal(match.group(0))
        cursor = match.end()
    literal(value[cursor:])
    return parts


def assess_transcript(body):
    """Return deterministic policy/decision/reason/counts; never mutate ``body``.

    Decisions are ``exclude``, ``keep`` and ``review``. Consumers should exclude
    only the first; ``review`` does not authorize removing or hiding content.
    Both ASR raw_text and punctuated text count as evidence of meaningful words.
    """
    counts = dict(segments=0, text_segments=0, meaningful_segments=0,
                  filler_segments=0, placeholder_segments=0, characters=0)

    def result(decision, reason):
        return dict(policy=POLICY, decision=decision, reason=reason,
                    reason_text=REASONS[reason], counts=counts)

    if not isinstance(body, dict):
        return result('review', 'invalid_transcript')
    if body.get('status') not in {'ready', 'no_speech'}:
        return result('review', 'transcript_incomplete')
    segments = body.get('segments')
    if not isinstance(segments, list):
        return result('review', 'invalid_transcript')
    counts['segments'] = len(segments)
    meaningful = []
    malformed = False
    for segment in segments:
        if not isinstance(segment, dict):
            malformed = True
            continue
        variants = []
        for field in ('text', 'raw_text'):
            value = segment.get(field, '')
            if value is None:
                value = ''
            if not isinstance(value, str):
                malformed = True
                continue
            if value and value not in variants:
                variants.append(value)
        parts = [part for value in variants for part in _parts(value)]
        if not parts:
            continue
        counts['text_segments'] += 1
        counts['characters'] += sum(len(p) for p, _ in parts)
        filler = any(not marker and set(p) <= FILLERS for p, marker in parts)
        placeholder = any(marker for _, marker in parts)
        content = [p for p, marker in parts if not marker and not set(p) <= FILLERS]
        counts['filler_segments'] += int(filler)
        counts['placeholder_segments'] += int(placeholder)
        if content:
            counts['meaningful_segments'] += 1
            meaningful.append(''.join(content))
    if malformed:
        return result('review', 'invalid_transcript')
    if meaningful:
        # Repeated ASR output can also be genuine speech. Flag only, never
        # exclude, and do not invent confidence scores absent from the engine.
        if len(meaningful) >= 6 and Counter(meaningful).most_common(1)[0][1] / len(meaningful) >= .85:
            return result('review', 'repetitive_text')
        return result('keep', 'meaningful_text')
    if not counts['text_segments']:
        return result('exclude', 'no_recognized_text')
    if counts['filler_segments'] and counts['placeholder_segments']:
        return result('exclude', 'filler_and_unintelligible_only')
    if counts['placeholder_segments']:
        return result('exclude', 'unintelligible_only')
    return result('exclude', 'filler_only')
