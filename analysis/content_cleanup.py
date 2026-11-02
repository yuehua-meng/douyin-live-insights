"""Reversible cleanup of saved outputs that only cite excluded speech content."""
import argparse
import copy
import json
import time

from .content_quality import assess_transcript
from .naming import enrich_result
from .store import packed


def clean_result(result, transcripts):
    """Never discard a mixed recording/job merely because some speech is empty."""
    invalid=[t for t in transcripts if assess_transcript(t)['decision']=='exclude']
    valid=[t for t in transcripts if assess_transcript(t)['decision']!='exclude']
    if not invalid:
        return result,False
    if not valid:
        return result,True
    good_recordings={t.get('recording',{}).get('id') for t in valid}
    bad_recordings={t.get('recording',{}).get('id') for t in invalid}-good_recordings
    output=copy.deepcopy(result)
    removed=False
    for kind,field in [('summary','items'),('script','cases')]:
        content=output.get(kind)
        if not isinstance(content,dict):continue
        evidence={e['id']:e for e in content.get('evidence',[]) if isinstance(e,dict) and e.get('id')}
        def entirely_bad(item):
            refs=item.get('refs',[])
            return bool(refs) and all(r in evidence and evidence[r].get('recording_id') in bad_recordings for r in refs)
        old=content.get(field,[])
        content[field]=[item for item in old if not entirely_bad(item)]
        if len(old)==len(content[field]):continue
        removed=True
        if not content[field]:output.pop(kind)
        elif kind=='script':content['chars']=sum(not c.isspace() for item in content[field] for c in item.get('text',''))
    if not removed:
        return result,False
    if not any(output.get(k) for k in ('summary','script')):
        return result,True
    output.pop('display_title',None)
    return enrich_result(output),False


def cleanup(store, apply=False):
    with store.db() as db:
        transcripts=[json.loads(row[0]) for row in db.execute('SELECT body FROM transcripts ORDER BY created DESC,rowid DESC')]
        jobs=[store.unpack(row) for row in db.execute("SELECT * FROM jobs WHERE state NOT IN ('queued','running') AND id NOT IN (SELECT job_id FROM job_archives)")]
    latest={}
    for body in transcripts:latest.setdefault(store.content_version(body.get('recording',{})),body)
    by_id={t['id']:t for t in transcripts}
    report=dict(applied=apply,checked_recordings=len(latest),excluded_recordings=[],archived_jobs=[],pruned_jobs=[])
    for body in latest.values():
        quality=store.assess_content(body) if apply else assess_transcript(body)
        if quality['decision']=='exclude':
            report['excluded_recordings'].append(dict(recording=body.get('recording',{}),transcript_id=body['id'],quality=quality))
    for job in jobs:
        result=job['result']
        if not any(isinstance(result.get(k),dict) for k in ('summary','script')):continue
        ids=result.get('transcript_ids') or job['payload'].get('transcript_ids',[])
        if not ids or any(tid not in by_id for tid in ids):continue
        updated,archive=clean_result(result,[by_id[tid] for tid in ids])
        if not archive and updated==result:continue
        report['archived_jobs' if archive else 'pruned_jobs'].append(job['id'])
        if apply:
            with store.db(True) as db:
                db.execute('INSERT OR IGNORE INTO content_output_backups VALUES(?,?,?,?)',
                    (job['id'],packed(result),'transcript-content-v1',time.time()))
                if archive:
                    db.execute('INSERT OR IGNORE INTO job_archives VALUES(?,?,?)',
                        (job['id'],'content_filter: 没有有效文字或成果仅引用无效语音',time.time()))
                else:
                    db.execute('UPDATE jobs SET result=?,updated=? WHERE id=?',(packed(updated),time.time(),job['id']))
    report['excluded_count']=len(report['excluded_recordings'])
    return report


def main():
    from .settings import Settings
    from .store import Store
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(cleanup(Store(Settings.load().data),args.apply),ensure_ascii=True))


if __name__=='__main__':main()
