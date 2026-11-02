"""Read-only, per-case library of published narration and its source summary.

Case identity is separate from job/version identity. In particular a title is
never enough evidence that two customers or projects are the same project.
"""
import copy
import json
import re

from .naming import case_name
from .store import digest


def _ids(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = []
    return sorted({x for x in (value or []) if isinstance(x, str) and x}) if isinstance(value, list) else []


def _items(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = []
    return [x for x in (value or []) if isinstance(x, dict) and str(x.get('text') or '').strip()] if isinstance(value, list) else []


def _identity(row, case):
    project = str(case.get('case_id') or '').strip()
    if project and row['collection_id']:
        identity = ['collection-case', row['collection_id'], project]
    elif project and row['tids']:
        # Explicit project identity inside the exact same source versions.
        identity = ['source-case', row['tids'], project]
    elif project:
        identity = ['job-case', row['job_id'], project]
    elif row['tids']:
        # Old drafts have no durable project IDs. Only collapse exact copies
        # from identical sources; matching titles alone must never merge them.
        identity = ['legacy-copy', row['tids'], case['text'].strip()]
    else:
        identity = ['job-position', row['job_id'], row['case_index']]
    return 'case_' + digest(identity)[:32]


def _published(case):
    return (isinstance(case, dict) and isinstance(case.get('text'), str)
            and bool(case['text'].strip()) and not case.get('draft')
            and case.get('state') not in {'draft', 'writing', 'reviewing', 'pending'})


def _summary_index(store):
    """Legacy script and summary jobs were separate; match exact input IDs."""
    with store.db() as db:
        rows = db.execute("""SELECT j.id job_id,j.created,j.updated,
            COALESCE(a.locked,0) locked,
            COALESCE(json_extract(j.result,'$.summary.transcript_ids'),
              json_extract(j.result,'$.transcript_ids'),json_extract(j.payload,'$.transcript_ids'),'[]') tids
            FROM jobs j LEFT JOIN adopted_outputs a ON a.job_id=j.id
            WHERE j.id NOT IN (SELECT job_id FROM job_archives)
              AND COALESCE(json_extract(j.result,'$.content_filtered'),0)=0
              AND json_array_length(j.result,'$.summary.items')>0
            ORDER BY COALESCE(a.locked,0) DESC,j.created DESC,j.updated DESC,j.id DESC""").fetchall()
    index = {}
    for row in rows:
        key = tuple(_ids(row['tids']))
        if key:
            index.setdefault(key, row['job_id'])
    return index


def _selected(store):
    # The list reads authored cases and metadata, not all transcript/evidence
    # bodies. No fixed 100/200-row limit may silently hide older useful cases.
    with store.db() as db:
        rows = db.execute("""SELECT j.id job_id,j.mode,j.state,j.created,j.updated,
            json_extract(j.payload,'$.collection_id') collection_id,
            COALESCE(json_extract(j.result,'$.script.transcript_ids'),
              json_extract(j.result,'$.transcript_ids'),json_extract(j.payload,'$.transcript_ids'),'[]') tids,
            COALESCE(a.locked,0) locked,
            COALESCE(json_array_length(j.result,'$.summary.items'),0)>0 has_summary,
            c.key case_index,c.value case_body
            FROM jobs j LEFT JOIN adopted_outputs a ON a.job_id=j.id,
              json_each(j.result,'$.script.cases') c
            WHERE j.id NOT IN (SELECT job_id FROM job_archives)
              AND COALESCE(json_extract(j.result,'$.content_filtered'),0)=0
            ORDER BY j.created DESC,j.updated DESC,j.id DESC,c.key ASC""").fetchall()
    selected = {}
    summaries = _summary_index(store)
    for raw in rows:
        row = dict(raw)
        try:
            case = json.loads(row.pop('case_body'))
        except (ValueError, TypeError):
            continue
        if not _published(case):
            continue
        row['tids'] = _ids(row['tids'])
        row['case_index'] = int(row['case_index'])
        row['locked'] = bool(row['locked'] or case.get('locked') or case.get('adopted'))
        key = _identity(row, case)
        row['id'] = key
        row['case'] = case
        row['summary_job_id'] = row['job_id'] if row['has_summary'] else summaries.get(tuple(row['tids']))
        priority = (row['locked'], row['created'], row['updated'], row['job_id'])
        existing = selected.get(key)
        if existing is None or priority > existing['priority']:
            row['priority'] = priority
            selected[key] = row
    return sorted(selected.values(), key=lambda row: (row['created'], row['updated'], row['id']), reverse=True)


def _metadata(row):
    case = row['case']
    title = case_name(case, {}, row['case_index'])
    # Publication checks can update job.updated without changing one word.
    # Daily delivery must deduplicate actual authored versions instead.
    version = digest({k: case.get(k) for k in ('title', 'stage', 'text', 'refs', 'missing_details')})
    return dict(id=row['id'], job_id=row['job_id'], case_index=row['case_index'],
                title=title, stage=case.get('stage') or '未明确',
                created=row['created'], updated=row['updated'],
                excerpt=re.sub(r'\s+', ' ', case['text']).strip()[:160],
                summary_available=bool(row['summary_job_id']), locked=row['locked'],
                partial=row['state'] != 'completed', version=version)


def list_cases(store):
    """Return metadata for every distinct published case, newest first."""
    return [_metadata(row) for row in _selected(store)]


def _source_key(evidence):
    if not isinstance(evidence, dict) or not evidence.get('recording_id'):
        return None
    if evidence.get('start_ms') is None or evidence.get('end_ms') is None:
        return None
    return (evidence['recording_id'], evidence['start_ms'], evidence['end_ms'])


def _associated_summary(case, script, summary, same_job):
    if not isinstance(summary, dict) or not _items(summary.get('items')):
        return None, 'unavailable'
    wanted = set(_ids(case.get('refs')))
    source_keys = {_source_key(e) for e in script.get('evidence', [])
                   if isinstance(e, dict) and e.get('id') in wanted}
    source_keys.discard(None)
    related_refs = set(wanted) if same_job else set()
    related_refs.update(e['id'] for e in summary.get('evidence', [])
                        if isinstance(e, dict) and e.get('id') and _source_key(e) in source_keys)
    related = [item for item in _items(summary.get('items')) if related_refs & set(_ids(item.get('refs')))]
    result = copy.deepcopy(summary)
    if related:
        result['items'] = copy.deepcopy(related)
        result['title'] = case.get('title') or result.get('title')
        used = {ref for item in related for ref in _ids(item.get('refs'))}
        result['evidence'] = [e for e in result.get('evidence', []) if isinstance(e, dict) and e.get('id') in used]
        return result, 'case'
    # No invented item-to-project association: the frontend must explicitly
    # label this as the shared source batch and it may discuss other projects.
    return result, 'batch'


def get_case(store, case_key):
    """Fetch one published case with its evidence and corresponding summary."""
    row = next((row for row in _selected(store) if row['id'] == case_key), None)
    if row is None:
        return None
    job = store.job(row['job_id'])
    if not job:
        return None
    result = job.get('result') or {}
    script = copy.deepcopy(result.get('script') or {})
    case = copy.deepcopy(row['case'])
    case['title'] = case_name(case, result, row['case_index'])
    script['cases'] = [case]
    script['chars'] = sum(not c.isspace() for c in case['text'])
    script.pop('processing', None)
    script.pop('evaluations', None)
    summary_job = job if row['summary_job_id'] == job['id'] else store.job(row['summary_job_id']) if row['summary_job_id'] else None
    summary, scope = _associated_summary(case, script,
        (summary_job.get('result') or {}).get('summary') if summary_job else None,
        same_job=bool(summary_job and summary_job['id'] == job['id']))
    return dict(_metadata(row), case=case, script=script, summary=summary,
                summary_scope=scope, summary_job_id=row['summary_job_id'])
