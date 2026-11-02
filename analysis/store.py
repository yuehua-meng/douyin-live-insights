from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import sqlite3
import time
import uuid

from .naming import enrich_result


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


class Cancelled(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.sqlite3"
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, mode TEXT NOT NULL,
                    payload TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
                    progress TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', cancel INTEGER NOT NULL DEFAULT 0,
                    owner TEXT, lease REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS transcripts(
                    id TEXT PRIMARY KEY, cache_key TEXT UNIQUE, body TEXT NOT NULL,
                    parent TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints(
                    cache_key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS summaries(
                    cache_key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usage(
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, day TEXT NOT NULL,
                    state TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER,
                    input_tokens INTEGER, output_tokens INTEGER, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts(peer TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS job_archives(
                    job_id TEXT PRIMARY KEY, reason TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS adopted_outputs(
                    job_id TEXT PRIMARY KEY, locked INTEGER NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS content_quality_records(
                    recording_key TEXT PRIMARY KEY, recording_id TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns TEXT NOT NULL, transcript_id TEXT NOT NULL,
                    decision TEXT NOT NULL, quality TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS content_output_backups(
                    job_id TEXT PRIMARY KEY, result TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
            ''')
        with self.db(True) as db:
            if 'billing_group' not in {r[1] for r in db.execute('PRAGMA table_info(usage)')}:
                db.execute('ALTER TABLE usage ADD COLUMN billing_group TEXT')

    @contextmanager
    def db(self, write=False):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def unpack(row):
        if row is None:
            return None
        value = dict(row)
        for field in ("payload", "progress", "result"):
            value[field] = json.loads(value[field])
        value.pop("owner", None)
        value.pop("lease", None)
        return value

    def create_job(self, mode, payload):
        key = digest([mode, payload])
        now = time.time()
        with self.db(True) as db:
            existing = db.execute("SELECT * FROM jobs WHERE fingerprint=?", (key,)).fetchone()
            if existing:
                return self.unpack(existing)
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 20:
                raise ValueError("任务队列已满，请等待现有任务完成")
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,fingerprint,mode,payload,stage,state,created,updated) VALUES(?,?,?,?,?,'queued',?,?)",
                       (job_id, key, mode, packed(payload), "summarizing" if mode in {"summary", "script"} or payload.get('transcript_ids') else "transcribing", now, now))
            return self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def job(self, job_id):
        with self.db() as db:
            return self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self):
        with self.db() as db:
            return [self.unpack(r) for r in db.execute("SELECT * FROM jobs WHERE id NOT IN (SELECT job_id FROM job_archives) ORDER BY created DESC LIMIT 100")]

    def locked(self, job_id):
        with self.db() as db:
            row=db.execute('SELECT locked FROM adopted_outputs WHERE job_id=?',(job_id,)).fetchone()
        return bool(row and row[0])

    def job_headers(self):
        with self.db() as db:
            rows=db.execute("""SELECT id,mode,state,stage,created,updated,progress,error,cancel,attempts,
                json_extract(payload,'$.collection_id') AS collection_id,
                COALESCE(json_extract(result,'$.content_filtered'),0) AS content_filtered,
                COALESCE(json_extract(payload,'$.automatic'),0) AS automatic
                FROM jobs WHERE id NOT IN (SELECT job_id FROM job_archives)
                ORDER BY created DESC LIMIT 100""").fetchall()
        headers=[]
        for r in rows:
            value=dict(r);value['progress']=json.loads(value['progress']);headers.append(value)
        return headers

    def lock_output(self, job_id, locked):
        with self.db(True) as db:
            db.execute('INSERT OR REPLACE INTO adopted_outputs VALUES(?,?,?)',(job_id,int(locked),time.time()))

    def feed(self):
        with self.db() as db:
            rows=db.execute("""SELECT j.id AS job_id,j.mode,j.state,j.created,j.updated,j.progress,j.error,
              json_extract(payload,'$.collection_id') AS collection_id,
              COALESCE(json_extract(result,'$.transcript_ids'),json_extract(payload,'$.transcript_ids'),'[]') AS tids,
              COALESCE(NULLIF(json_extract(result,'$.display_title'),''),NULLIF(json_extract(result,'$.script.cases[0].title'),''),
                CASE WHEN json_type(result,'$.summary')='object' THEN
                  '商业总结｜' || COALESCE(NULLIF(json_extract(result,'$.summary.title'),''),NULLIF(json_extract(result,'$.summary.items[0].heading'),''),'待命名主题')
                ELSE '口播待补充｜尚未形成项目案例' END) AS title,
              json_type(result,'$.summary')='object' AS summary_ready,
              json_type(result,'$.script')='object' AS script_ready,
              COALESCE(json_array_length(result,'$.script.cases'),0) AS case_count,
              COALESCE(json_extract(result,'$.script.processing.processed'),0) AS case_processed,
              COALESCE(a.locked,0) AS locked
              FROM jobs j LEFT JOIN adopted_outputs a ON a.job_id=j.id
              WHERE j.id NOT IN (SELECT job_id FROM job_archives) AND
              (json_type(result,'$.summary')='object' OR json_type(result,'$.script')='object')
              ORDER BY j.created DESC LIMIT 200""").fetchall()
        entries,seen=[],set()
        # A retained version remains the default for its collection. New versions
        # remain in history and become visible when the retained version is released.
        locked={(r['collection_id'] or digest(sorted(json.loads(r['tids'])))):r['job_id'] for r in reversed(rows) if r['locked']}
        # Legacy summary and script jobs were separate. Prefer the existing
        # named case as their shared entry; a later summary must not hide it.
        # New business results deliberately keep their own eligibility outcome.
        business_keys={r['collection_id'] or digest(sorted(json.loads(r['tids']))) for r in rows if r['mode']=='business'}
        legacy_cases={}
        for r in rows:
            key=r['collection_id'] or digest(sorted(json.loads(r['tids'])))
            if key not in business_keys and r['case_count']:
                legacy_cases.setdefault(key,r['job_id'])
        for row in rows:
            entry=dict(row);ids=json.loads(entry.pop('tids'))
            key=entry['collection_id'] or digest(sorted(ids))
            if key in seen:continue
            if key in locked and locked[key]!=entry['job_id']:continue
            if key not in locked and key in legacy_cases and legacy_cases[key]!=entry['job_id']:continue
            seen.add(key)
            entry.update(transcript_ids=ids,summary_ready=bool(entry['summary_ready']),script_ready=bool(entry['script_ready']),
                         locked=bool(entry['locked']),partial=entry['state']!='completed',coverage={'done':len(ids),'total':len(ids)})
            entry['progress']=json.loads(entry['progress'])
            entries.append(entry)
        return entries

    def archive_script_jobs(self, ids, reason):
        with self.db(True) as db:
            for job_id in ids:
                row = db.execute('SELECT mode,state FROM jobs WHERE id=?', (job_id,)).fetchone()
                if not row or row['mode'] != 'script' or row['state'] != 'completed':
                    raise ValueError('只能归档已完成的口播任务')
                db.execute('INSERT OR IGNORE INTO job_archives VALUES(?,?,?)', (job_id, reason, time.time()))

    def result_catalog(self):
        # Metadata only: opening the dashboard does not download every transcript
        # and generated body, or trigger any model requests.
        with self.db() as db:
            rows = db.execute("""SELECT id, created,
                json_type(result,'$.summary') AS has_summary,
                json_type(result,'$.script') AS has_script,
                json_array_length(result,'$.script.cases') AS case_count,
                json_extract(payload,'$.script_topic') AS topic,
                json_extract(result,'$.script.cases[0].title') AS title,
                json_extract(result,'$.summary.title') AS summary_title,
                json_extract(result,'$.summary.transcript_ids') AS summary_ids,
                json_extract(result,'$.script.transcript_ids') AS script_ids,
                json_extract(result,'$.transcript_ids') AS fallback_ids
                FROM jobs WHERE state='completed' AND id NOT IN (SELECT job_id FROM job_archives) AND
                (json_type(result,'$.summary')='object' OR json_type(result,'$.script')='object')
                ORDER BY created DESC, id DESC LIMIT 200""").fetchall()
        entries, seen_scripts = [], set()
        for row in rows:
            for kind in ('summary', 'script'):
                if row['has_'+kind] != 'object':
                    continue
                title = str(row['title'] or '').strip() if kind == 'script' else '商业总结｜'+str(row['summary_title'] or '待命名主题')
                ids = json.loads(row[kind+'_ids'] or row['fallback_ids'] or '[]')
                if kind == 'script':
                    if not row['case_count']:
                        continue
                    topic = ''.join(str(row['topic'] or title).casefold().split())
                    key = (tuple(sorted(ids)), topic)
                    if key in seen_scripts:
                        continue
                    seen_scripts.add(key)
                entries.append(dict(job_id=row['id'], kind=kind, created=row['created'],
                    title=title or ('项目案例口播 · '+row['id'][:8]),
                    transcript_ids=ids))
        return entries

    def project_catalog(self):
        """Expose extracted business leads, including cases still under review."""
        with self.db() as db:
            rows=db.execute("""SELECT j.id job_id,j.updated,j.state,
                json_extract(j.payload,'$.collection_id') collection_id,p.value project
                FROM jobs j,json_each(j.result,'$.dossier.projects') p
                WHERE j.mode='business' AND j.id NOT IN (SELECT job_id FROM job_archives)
                ORDER BY j.updated DESC""").fetchall()
        found={}
        for row in rows:
            project=json.loads(row['project'])
            key=(row['collection_id'] or row['job_id'],project.get('id') or project.get('title'))
            if key in found:
                continue
            facts=project.get('facts') or []
            highlights=[]
            for kind in ('need','workflow','decision','delivery','result','commercial','value'):
                fact=next((f for f in facts if f.get('kind')==kind and f.get('nature')!='未确认'),None)
                if fact:
                    highlights.append(dict(kind=kind,text=str(fact.get('text') or '')[:240],nature=fact.get('nature')))
            found[key]=dict(job_id=row['job_id'],title=project.get('title') or '待命名项目',
                stage=project.get('stage') or '未明确',readiness=project.get('readiness') or '待核对',
                attribution=project.get('attribution') or '待核对',facts=len(facts),
                highlights=highlights,missing=(project.get('missing_details') or [])[:3],
                state=row['state'],updated=row['updated'])
        return sorted(found.values(),key=lambda p:p['updated'],reverse=True)

    def claim(self, stage):
        now = time.time()
        owner = uuid.uuid4().hex
        with self.db(True) as db:
            db.execute("""UPDATE jobs SET state=CASE WHEN cancel=1 THEN 'cancelled'
                WHEN stage='summarizing' AND json_extract(payload,'$.automatic')=1 AND EXISTS
                    (SELECT 1 FROM usage WHERE job_id=jobs.id AND state='uncertain') THEN 'failed'
                WHEN attempts>=3 THEN 'failed' ELSE 'queued' END, owner=NULL,
                error='执行进程中断；未决模型请求需核对，已保存检查点',updated=? WHERE state='running' AND lease<?""", (now, now))
            row = db.execute("SELECT * FROM jobs WHERE stage=? AND state='queued' AND cancel=0 ORDER BY created LIMIT 1", (stage,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',owner=?,lease=?,attempts=attempts+1,updated=? WHERE id=?", (owner, now+90, now, row['id']))
            return row['id'], owner

    def heartbeat(self, job_id, owner):
        with self.db(True) as db:
            row = db.execute("SELECT cancel FROM jobs WHERE id=? AND owner=? AND state='running'", (job_id, owner)).fetchone()
            if not row or row['cancel']:
                raise Cancelled()
            db.execute("UPDATE jobs SET lease=?,updated=? WHERE id=? AND owner=?", (time.time()+90, time.time(), job_id, owner))

    def update(self, job_id, owner, **fields):
        allowed = {"progress", "result", "stage", "state", "error"}
        assert set(fields) <= allowed
        if 'result' in fields:
            fields['result'] = enrich_result(fields['result'])
        fields = {k: packed(v) if k in {"progress", "result"} else v for k, v in fields.items()}
        fields['updated'] = time.time()
        with self.db(True) as db:
            query = ",".join(k+"=?" for k in fields)
            count = db.execute(f"UPDATE jobs SET {query} WHERE id=? AND owner=?", (*fields.values(), job_id, owner)).rowcount
            if not count:
                raise Cancelled()

    def cancel(self, job_id):
        with self.db(True) as db:
            db.execute("UPDATE jobs SET cancel=1,state=CASE WHEN state='running' THEN state ELSE 'cancelled' END,updated=? WHERE id=? AND state!='completed'", (time.time(), job_id))

    def retry(self, job_id):
        with self.db(True) as db:
            if not db.execute("UPDATE jobs SET state='queued',cancel=0,error='',owner=NULL,attempts=0,updated=? WHERE id=? AND state IN ('failed','cancelled','awaiting_budget')", (time.time(), job_id)).rowcount:
                raise ValueError("此任务当前不能重试")

    def cached(self, table, key):
        assert table in {"checkpoints", "summaries", "transcripts"}
        with self.db() as db:
            row = db.execute(f"SELECT body FROM {table} WHERE cache_key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def cache(self, table, key, body):
        assert table in {"checkpoints", "summaries"}
        with self.db(True) as db:
            db.execute(f"INSERT OR REPLACE INTO {table}(cache_key,body) VALUES(?,?)", (key, packed(body)))

    def transcript(self, transcript_id):
        with self.db() as db:
            row = db.execute("SELECT body FROM transcripts WHERE id=?", (transcript_id,)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def content_version(recording):
        return (recording.get('id'),recording.get('size'),str(recording.get('mtime_ns')))

    def assess_content(self, body):
        from .content_quality import assess_transcript
        quality=assess_transcript(body)
        recording=body.get('recording',{}) if isinstance(body,dict) else {}
        rid,size,mtime=self.content_version(recording)
        if not rid or size is None or mtime=='None' or not body.get('id'):
            return quality
        with self.db(True) as db:
            # The caller can be an old job/cache. Only the newest saved version
            # may decide whether the physical recording is hidden globally.
            latest=db.execute("""SELECT id,body FROM transcripts WHERE
                json_extract(body,'$.recording.id')=? AND json_extract(body,'$.recording.size')=?
                AND CAST(json_extract(body,'$.recording.mtime_ns') AS TEXT)=?
                ORDER BY created DESC,rowid DESC LIMIT 1""",(rid,size,mtime)).fetchone()
            if latest:
                persisted=quality if latest['id']==body['id'] else assess_transcript(json.loads(latest['body']))
                key=digest([rid,size,mtime])
                existing=db.execute('SELECT transcript_id,quality FROM content_quality_records WHERE recording_key=?',(key,)).fetchone()
                if existing and existing['transcript_id']==latest['id'] and existing['quality']==packed(persisted):
                    return quality
                db.execute('INSERT OR REPLACE INTO content_quality_records VALUES(?,?,?,?,?,?,?,?)',
                    (key,rid,size,mtime,latest['id'],persisted['decision'],packed(persisted),time.time()))
        return quality

    def content_filter_keys(self):
        with self.db() as db:
            return {(r['recording_id'],r['size'],r['mtime_ns']) for r in db.execute(
                "SELECT recording_id,size,mtime_ns FROM content_quality_records WHERE decision='exclude'")}

    def content_filtered(self, recording):
        return self.content_version(recording) in self.content_filter_keys()

    def content_filter_counts(self):
        with self.db() as db:
            rows=db.execute("SELECT decision,count(*) n FROM content_quality_records GROUP BY decision").fetchall()
        result={r['decision']:r['n'] for r in rows}
        return dict(excluded=result.get('exclude',0),review=result.get('review',0),kept=result.get('keep',0))

    def transcribed_recordings(self):
        # Read only compact metadata, not transcript bodies or audio. A batch
        # failure (or a summary failure) does not undo completed transcription.
        with self.db() as db:
            rows = db.execute("""SELECT DISTINCT
                json_extract(body, '$.recording.id') AS id,
                json_extract(body, '$.recording.size') AS size,
                json_extract(body, '$.recording.mtime_ns') AS mtime_ns
                FROM transcripts WHERE json_extract(body, '$.status')='ready'
                AND json_array_length(body, '$.segments')>0""").fetchall()
        # Use a string for nanoseconds: browser Numbers lose precision here.
        return [dict(id=r['id'], size=r['size'], mtime_ns=str(r['mtime_ns']))
                for r in rows if r['id'] and r['size'] is not None and r['mtime_ns'] is not None]

    def save_transcript(self, body, key=None, parent=None):
        with self.db(True) as db:
            db.execute("INSERT OR IGNORE INTO transcripts VALUES(?,?,?,?,?)", (body['id'], key, packed(body), parent, time.time()))

    def reserve(self, job_id, amount, settings):
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        with self.db(True) as db:
            expression = "COALESCE(SUM(COALESCE(actual,reserved)),0)"
            daily = db.execute(f"SELECT {expression} FROM usage WHERE day=?", (day,)).fetchone()[0]
            group,seen=job_id,set()
            while group not in seen:
                seen.add(group)
                row=db.execute("SELECT json_extract(payload,'$.billing_group_id') FROM jobs WHERE id=?",(group,)).fetchone()
                if not row or not row[0]:break
                group=row[0]
            task = db.execute(f"SELECT {expression} FROM usage WHERE COALESCE(billing_group,job_id)=?", (group,)).fetchone()[0]
            if settings.task_budget <= 0 or task+amount > settings.task_budget:
                raise BudgetExceeded("本批预算不足；已完成内容保留，请调整后继续")
            if settings.daily_budget <= 0 or daily+amount > settings.daily_budget:
                raise BudgetExceeded("每日预算不足；已完成内容保留，等待下一预算日")
            usage_id = uuid.uuid4().hex
            db.execute("INSERT INTO usage(id,job_id,day,state,reserved,created,billing_group) VALUES(?,?,?,'uncertain',?,?,?)", (usage_id, job_id, day, amount, time.time(),group))
            return usage_id

    def settle(self, usage_id, amount, inputs=0, outputs=0):
        with self.db(True) as db:
            db.execute("UPDATE usage SET state='settled',actual=?,input_tokens=?,output_tokens=? WHERE id=?", (amount, inputs, outputs, usage_id))

    def spending(self):
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        with self.db() as db:
            return dict(db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) as committed,COALESCE(SUM(state='uncertain'),0) as uncertain_calls FROM usage WHERE day=?", (day,)).fetchone())
