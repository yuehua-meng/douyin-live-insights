"""Local, grounded display names. This module never calls a language model."""
import argparse
import copy
import json
import re


VERSION = 'content-names-v2'
GENERIC = {
    '', '项目案例', '产品案例', '案例', '案例口播', '项目案例口播', '项目案例口播稿',
    '产品案例口播稿', '口播稿', '口播', '录音内容成果', '录音内容总结', '商业总结',
    '录音总结', '内容总结', '总结', '概览', '主题', '要点', '问答', '行动项',
    '需核实', '事实', '观点', '建议', '行动', '业务要点', '商业要点', '信息缺口',
    '客户需求', '商业逻辑', '业务流程', '交付过程', '项目结果', '经验复盘',
    '分歧', '争议', '分歧与需核实', '待核实', '需核实信息', '补充说明',
    '材料情况', '材料概况', '信息不足', '待核实', '原文内容', '主题概括',
    '总体概览', '整体概览', '内容概览', '主要观点', '素材信息不足',
}


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip(' \t\r\n#*“”"《》')


def concrete(value):
    text = clean(value)
    base = re.sub(r'\s*[·｜|:：]\s*[0-9a-f]{6,32}$', '', text, flags=re.I)
    base = re.sub(r'\s*[（(]?\d+[）)]?$', '', base).strip()
    return bool(text and base not in GENERIC)


def short(value, limit=34):
    text = clean(value).rstrip('，。；、：,:;.!！?？')
    return text if len(text) <= limit else text[:limit-1].rstrip()+'…'


def excerpt(value):
    """Use an actual clause as a name; never infer delivery, results or a client."""
    clauses = re.split(r'[\n。！？!?；;]+', str(value or '')[:3000])
    candidates = []
    for index, raw in enumerate(clauses):
        text = clean(raw)
        text = re.sub(r'^(?:首先|其次|最后|概览|主题|要点)[：:、，\s]*', '', text)
        text = re.sub(r'^(?:今天(?:我|我们)?(?:想|来)?(?:给大家)?(?:分享|聊聊|讲讲)|下面(?:我|我们)?(?:来)?(?:分享|聊聊))', '', text)
        text = text.strip('，,：: ')
        if len(text) < 4 or not concrete(text):
            continue
        # Names based on a customer, need or product are easier to find than
        # conversational introductions, but every word still comes from text.
        score = min(len(text), 20) / 20 - index / 25
        score += sum(word in text for word in ('客户', '项目', '业务', '产品', '需求', '咨询', '销售', '申报', '方案'))
        if re.fullmatch(r'(?:一个|一下|我们)?(?:完整的|真实的|具体的|有意思的)?(?:项目|案例|故事)[，,]?.{0,3}', text):
            continue
        candidates.append((score, -index, text))
    return short(max(candidates)[2]) if candidates else ''


def _projects(result):
    dossier = result.get('dossier')
    return [p for p in dossier.get('projects', []) if isinstance(p, dict)] if isinstance(dossier, dict) else []


def case_name(case, result, index):
    if concrete(case.get('title')):
        return case['title']  # Preserve the exact authored title, including spacing.
    projects = _projects(result)
    matched = next((p for p in projects if case.get('case_id') and p.get('id') == case['case_id']), None)
    if matched and concrete(matched.get('title')):
        return short(matched['title'], 60)
    # A requested topic can be broader than the returned case, so do not copy
    # payload.script_topic onto an unnamed case or a whole-recording summary.
    title = excerpt(case.get('text'))
    if title:
        return title
    refs = set(case.get('refs') or [])
    script = result.get('script') or {}
    source = '。'.join(str(e.get('text') or '') for e in script.get('evidence', [])
                      if isinstance(e, dict) and e.get('id') in refs)
    return excerpt(source) or f'口播稿 {index+1}（主题待核对）'


def _join_topics(topics, limit=40):
    topics = list(dict.fromkeys(t for t in topics if t))[:2]
    if not topics:
        return '素材信息不足'
    if len(topics) == 1:
        return short(topics[0], limit)
    remaining = limit-3
    first_limit = max(1, remaining-min(len(topics[1]), remaining//2))
    first = short(topics[0], first_limit)
    return first+' / '+short(topics[1], remaining-len(first))


def _business_phrases(value):
    """Extract quoted-in-place subject phrases, not a summary of the summary."""
    text = clean(value)
    # A summary often starts with bookkeeping such as "the first half covers".
    # Start at its topic marker instead of making that bookkeeping the name.
    marker = re.search(r'(?:围绕|主要讨论(?:了)?|讨论(?:了|的是)?|涉及(?:了|的)?|聚焦(?:于)?|关注(?:的是)?|谈到(?:了)?|需求是|目标是)', text)
    if marker:
        text = text[marker.end():]
    parts = re.split(r'[\n。！？!?；;，,、]+', text[:2500])
    result = []
    for raw in parts:
        phrase = clean(raw).strip('：: …')
        phrase = re.sub(r'^(?:前半段|后半段|本段|整场直播|这段录音|主要|则|以及|和)[：:\s]*', '', phrase)
        phrase = re.sub(r'^(?:客户|用户)(?:需要|希望)(?:一个|通过)?', '', phrase)
        phrase = re.sub(r'^(?:这段|本段|整场|录音|直播).{0,10}(?:内容|分两段|分为两段)[：:]?', '', phrase)
        if (len(phrase) < 4 or not concrete(phrase)
                or re.search(r'(?:没有|无|缺乏|未(?:能|发现|包含|提供)?).{0,8}(?:实质|有效|具体|可识别|业务).{0,5}(?:内容|信息|需求|主题)', phrase)
                or re.search(r'(?:仅|只有|主要是|只包含).{0,5}(?:嗯|啊|语气词|寒暄|杂音|噪音)', phrase)
                or re.fullmatch(r'[嗯啊哦呃唉哈好的是对\s“”".…]+', phrase)):
            continue
        # Keep actual business subjects. Statements only about the recording or
        # lack of evidence should not become a purported project title.
        if not re.search(r'(?:AI|IP|账号|智能|产品|项目|业务|客户|用户|需求|销售|申报|咨询|企业|医疗|融资|资产|软件|控制|合规|协作|报告|资料|制造|物流|订单|财务|教育|营销|电商|采购|供应|客服|经营|成本|流程|方案|数据)', phrase, re.I):
            continue
        result.append(phrase)
    return result


def summary_name(summary, regenerate=False):
    if not regenerate and concrete(summary.get('title')):
        return short(summary['title'], 40)
    items = [item for item in summary.get('items', []) if isinstance(item, dict)]
    topics = []
    # Scan *all* headings before considering overview prose. A generic first
    # item must not crowd out actual subject headings further down the report.
    for item in items:
        heading = re.sub(r'^(?:话题|主题)[：:]\s*', '', clean(item.get('heading')))
        heading = re.sub(r'^[一二三四五六七八九十\d]+[、.．]\s*', '', heading)
        if not concrete(heading):
            continue
        # Long headings sometimes append an entire conclusion after a colon.
        subject = re.split(r'[：:]', heading, maxsplit=1)[0]
        if len(heading) > 20 and len(subject) >= 6 and concrete(subject):
            heading = subject
        topics.append(heading)
    if topics:
        return _join_topics(topics)
    for item in items:
        topics.extend(_business_phrases(item.get('text')))
    return _join_topics(topics)


def enrich_result(result):
    """Return a copy with names only; all prose, evidence and job identity survive."""
    if not isinstance(result, dict) or not any(isinstance(result.get(k), dict) for k in ('script', 'summary')):
        return result
    output = copy.deepcopy(result)
    script = output.get('script')
    cases = [c for c in script.get('cases', []) if isinstance(c, dict)] if isinstance(script, dict) else []
    for index, case in enumerate(cases):
        case['title'] = case_name(case, output, index)
    summary = output.get('summary')
    if isinstance(summary, dict):
        # Replace old auto-generated titles using their original items. Authored
        # titles without our naming marker are preserved within the UI budget.
        summary['title'] = summary_name(summary, regenerate=str(output.get('naming_version') or '').startswith('content-names-'))
    if cases:
        output['display_title'] = str(cases[0]['title']) + (f' 等 {len(cases)} 个案例' if len(cases) > 1 else '')
    elif isinstance(summary, dict):
        output['display_title'] = '商业总结｜'+summary['title']
    else:
        # An empty cases list is a completed analysis, not a generated case.
        names = [short(p.get('title')) for p in _projects(output) if concrete(p.get('title'))]
        output['display_title'] = '口播待补充｜'+(names[0] if names else '材料不足，未形成项目案例')
    output['naming_version'] = VERSION
    return output


def backfill(store, apply=False):
    """Idempotently rename terminal outputs, including archived metadata only."""
    counts = dict(checked=0, updated=0, renamed_cases=0, summary_only=0, applied=apply, titles=[])
    with store.db(write=apply) as db:
        rows = db.execute("""SELECT id,result FROM jobs WHERE state NOT IN ('queued','running') AND
            (json_type(result,'$.summary')='object' OR json_type(result,'$.script')='object')""").fetchall()
        for row in rows:
            body = json.loads(row['result']); output = enrich_result(body); counts['checked'] += 1
            if body == output:
                continue
            old = (body.get('script') or {}).get('cases', [])
            new = (output.get('script') or {}).get('cases', [])
            counts['renamed_cases'] += sum(a.get('title') != b.get('title') for a, b in zip(old, new))
            counts['summary_only'] += int(not new and isinstance(output.get('summary'), dict))
            counts['updated'] += 1
            counts['titles'].append(dict(job_id=row['id'], title=output['display_title']))
            if apply:
                # Do not change timestamps, state, locks, transcript versions,
                # fingerprints or model budgets for a display-only correction.
                db.execute('UPDATE jobs SET result=? WHERE id=?',
                           (json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(',', ':')), row['id']))
    return counts


def main():
    parser = argparse.ArgumentParser(description='Preview or apply local names to saved analysis results.')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from .settings import Settings
    from .store import Store
    print(json.dumps(backfill(Store(Settings.load().data), apply=args.apply), ensure_ascii=False))


if __name__ == '__main__':
    main()
