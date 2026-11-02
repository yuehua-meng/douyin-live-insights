"""Durable discovery and dispatch for newly finalized audio, never paid work here."""
import argparse
from datetime import datetime, timezone, timedelta
import json
import logging
import signal
import threading
import time

from .catalog import scan
from .settings import Settings
from .store import Store, digest, packed

LOG = logging.getLogger('analysis-scheduler')
GRACE = 600
UPDATE_SECONDS = 1800
MIN_AUDIO_MS = 1_790_000  # AAC framing can make a nominal 30-min file 29:59.
HISTORY_BATCH_MS = 3_600_000
HISTORY_BATCH_BYTES = 100_000
CLEAN_ENDS = {'stream_ended', 'manual_stop', 'test_limit', 'no_valid_audio'}


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


class Automation:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS automation_config(
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
                    activated_at REAL, last_scan REAL, last_error TEXT NOT NULL DEFAULT '');
                INSERT OR IGNORE INTO automation_config(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS automation_collections(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, started REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open', updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS automation_sessions(
                    session TEXT PRIMARY KEY, collection_id TEXT NOT NULL, owner TEXT NOT NULL,
                    started REAL NOT NULL, ended REAL, status TEXT NOT NULL, unfinished INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS automation_assets(
                    asset_key TEXT PRIMARY KEY, recording_id TEXT NOT NULL, collection_id TEXT NOT NULL,
                    recording TEXT NOT NULL, state TEXT NOT NULL, asr_job TEXT, transcript_id TEXT,
                    discovered REAL NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, retry_at REAL);
                CREATE TABLE IF NOT EXISTS automation_generations(
                    input_key TEXT PRIMARY KEY, collection_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    asset_keys TEXT NOT NULL, transcript_ids TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS automation_revisions(
                    collection_id TEXT PRIMARY KEY, due_at REAL NOT NULL, billing_job_id TEXT);
                CREATE TABLE IF NOT EXISTS automation_history_assets(
                    asset_key TEXT PRIMARY KEY, requested REAL NOT NULL,
                    reused_transcript INTEGER NOT NULL DEFAULT 0,
                    reused_asr INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS automation_history_collections(
                    collection_id TEXT PRIMARY KEY, snapshot_at REAL NOT NULL, closed INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS automation_assets_collection ON automation_assets(collection_id,state);
                CREATE UNIQUE INDEX IF NOT EXISTS automation_business_key
                    ON jobs(json_extract(payload,'$.generation_key')) WHERE mode='business';
                CREATE TABLE IF NOT EXISTS automation_daily_generations(
                    day TEXT NOT NULL,collection_id TEXT NOT NULL,job_id TEXT NOT NULL,
                    asset_keys TEXT NOT NULL,transcript_ids TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(day,collection_id));
            ''')
        if settings.daily_batch_mode:
            from .daily_digest import DailyDigest
            self.daily = DailyDigest(settings, store)
        else:
            self.daily = None

    def set_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('自动处理开关无效')
        with self.store.db(True) as db:
            db.execute('UPDATE automation_config SET enabled=? WHERE id=1', (int(enabled),))
        return self.status()

    def _manifests(self):
        root = self.settings.recordings.resolve()
        sessions = []
        for path in root.glob('*/*/*/session.json'):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            try:
                body = json.loads(path.read_text(encoding='utf-8'))
                started = timestamp(body.get('started_at'))
                # Missing/invalid clocks must not create a guessed end or merge.
                if started is None:
                    continue
                sessions.append(dict(session=path.parent.relative_to(root).as_posix(),
                    owner=str(body.get('douyin_id') or path.relative_to(root).parts[0]),
                    started=started, ended=timestamp(body.get('ended_at')),
                    status=str(body.get('status') or 'unknown'),
                    unfinished=bool(any(g.get('reason') == 'unfinished_segment' for g in body.get('gaps', []) if isinstance(g, dict))
                                    or list(path.parent.glob('*.pending'))),
                    touched=path.stat().st_mtime))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return sorted(sessions, key=lambda s: (s['started'], s['session']))

    def _register(self, sessions, recordings, now):
        with self.store.db(True) as db:
            config = dict(db.execute('SELECT * FROM automation_config WHERE id=1').fetchone())
            baseline = config['activated_at'] is None
            for session in sessions:
                existing = db.execute('SELECT collection_id FROM automation_sessions WHERE session=?', (session['session'],)).fetchone()
                if existing:
                    collection = existing[0]
                else:
                    previous = db.execute('SELECT * FROM automation_sessions WHERE owner=? AND started<=? ORDER BY started DESC LIMIT 1',
                                          (session['owner'], session['started'])).fetchone()
                    related = bool(previous and previous['ended'] is not None
                                   and -60 <= session['started'] - previous['ended'] <= GRACE)
                    # A still-recording manifest is positive evidence, not an
                    # offline timer. Bound stale manifests before linking a new
                    # connection, without using completed-file gaps to end it.
                    if previous and previous['status'] == 'recording':
                        prior = next((s for s in sessions if s['session'] == previous['session']), None)
                        related = bool(prior and session['started'] <= prior['touched'] + 3600)
                    collection = previous['collection_id'] if related else digest(['auto-collection-v1', session['owner'], session['session']])[:24]
                    db.execute('INSERT OR IGNORE INTO automation_collections(id,owner,started,updated) VALUES(?,?,?,?)',
                               (collection, session['owner'], session['started'], now))
                db.execute('INSERT OR REPLACE INTO automation_sessions VALUES(?,?,?,?,?,?,?)',
                           (session['session'], collection, session['owner'], session['started'], session['ended'], session['status'], int(session['unfinished'])))
            for recording in recordings:
                row = db.execute('SELECT collection_id FROM automation_sessions WHERE session=?', (recording['session'],)).fetchone()
                if not row:
                    continue
                key = digest([recording['id'], recording['size'], str(recording['mtime_ns'])])
                db.execute('INSERT OR IGNORE INTO automation_assets(asset_key,recording_id,collection_id,recording,state,discovered) VALUES(?,?,?,?,?,?)',
                           (key, recording['id'], row[0], packed(recording), 'baseline' if baseline else 'observed', now))
            if baseline:
                db.execute('UPDATE automation_config SET activated_at=? WHERE id=1', (now,))
            db.execute("UPDATE automation_config SET last_scan=?,last_error='' WHERE id=1", (now,))

    @staticmethod
    def _recording_version(recording):
        return (recording.get('id'), recording.get('size'), str(recording.get('mtime_ns')))

    def backfill_history(self, now=None):
        """Register an explicit history snapshot; repeated calls never reset work.

        Discovery is allowed while automatic dispatch is paused. No model request
        or ASR is performed here; the regular scheduler retains its queue limits.
        """
        now = time.time() if now is None else float(now)
        sessions = self._manifests()
        recordings = scan(self.settings.recordings, include_duration=True)
        self._register(sessions, recordings, now)
        counts = dict(selected=len(recordings), newly_registered=0, reused_transcripts=0,
                      reused_asr=0, kept_cancelled=0, kept_failed=0, restarted_failed=0)
        # Assess outside the registration transaction: Store persists the
        # exclusion independently and must not nest SQLite writers.
        transcripts = {}
        with self.store.db() as db:
            saved = db.execute("SELECT id,body FROM transcripts ORDER BY created DESC,rowid DESC").fetchall()
        for row in saved:
            body=json.loads(row['body'])
            if body.get('status') in {'ready','no_speech'}:
                transcripts.setdefault(self._recording_version(body.get('recording',{})),body)
        quality={body['id']:self.store.assess_content(body) for body in transcripts.values()}
        with self.store.db(True) as db:
            # Prefer the most recent saved body, including a user's corrected
            # revision, rather than the cached first ASR result.
            prior_asr = {}
            for row in db.execute("SELECT id,payload,state,stage FROM jobs WHERE state!='completed' ORDER BY created DESC,rowid DESC"):
                payload = json.loads(row['payload'])
                for recording in payload.get('recordings', []):
                    prior_asr.setdefault(self._recording_version(recording), dict(row))
            collection_ids = set()
            for recording in recordings:
                key = digest([recording['id'], recording['size'], str(recording['mtime_ns'])])
                asset = db.execute('SELECT * FROM automation_assets WHERE asset_key=?', (key,)).fetchone()
                if not asset:
                    continue
                collection_ids.add(asset['collection_id'])
                if db.execute('SELECT 1 FROM automation_history_assets WHERE asset_key=?', (key,)).fetchone():
                    continue
                counts['newly_registered'] += 1
                body = transcripts.get(self._recording_version(recording))
                prior = prior_asr.get(self._recording_version(recording))
                reused_transcript = reused_asr = 0
                # An already registered active/cancelled item is never reset by
                # a later "process history" request.
                if asset['state'] in {'baseline', 'observed'} and not asset['asr_job']:
                    if body:
                        state = 'no_speech' if quality[body['id']]['decision']=='exclude' else 'ready'
                        db.execute('UPDATE automation_assets SET state=?,transcript_id=? WHERE asset_key=?',
                                   (state, body['id'], key))
                        reused_transcript = 1
                        counts['reused_transcripts'] += 1
                    elif prior and (prior['state'] == 'cancelled' or
                                    (prior['stage'] == 'transcribing' and prior['state'] in {'queued','running'})):
                        state = 'cancelled' if prior['state'] == 'cancelled' else 'queued'
                        db.execute('UPDATE automation_assets SET state=?,asr_job=? WHERE asset_key=?',
                                   (state, prior['id'], key))
                        reused_asr = 1
                        counts['reused_asr'] += 1
                        if state == 'cancelled':
                            counts['kept_cancelled'] += 1
                    else:
                        # Explicit history processing authorizes a fresh ASR
                        # attempt after old bugs, without restarting the old
                        # combined task's paid summary phase. Later registrations
                        # never reset this new task's bounded retry allowance.
                        db.execute("UPDATE automation_assets SET state='observed' WHERE asset_key=?", (key,))
                        if prior and prior['state'] == 'failed':
                            counts['restarted_failed'] += 1
                db.execute('INSERT INTO automation_history_assets VALUES(?,?,?,?)',
                           (key, now, reused_transcript, reused_asr))
            for cid in collection_ids:
                rows = db.execute('SELECT * FROM automation_sessions WHERE collection_id=?', (cid,)).fetchall()
                # This closes the selected, already-ended historical snapshot;
                # it does not infer that a current room is offline. Invalid or
                # pending files remain excluded by catalog.scan.
                closed = bool(rows and all(r['status'] != 'recording' and r['ended'] is not None
                                           and r['ended'] <= now-GRACE for r in rows))
                db.execute('INSERT INTO automation_history_collections VALUES(?,?,?) ON CONFLICT(collection_id) '
                           'DO UPDATE SET snapshot_at=excluded.snapshot_at,closed=excluded.closed',
                           (cid, now, int(closed)))
        return dict(counts, status=self.status())

    def _history_closed(self, collection_id, sessions):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM automation_history_collections WHERE collection_id=?',
                             (collection_id,)).fetchone()
        return bool(row and row['closed'] and sessions and all(
            s['status'] != 'recording' and s['ended'] is not None
            and s['ended'] <= row['snapshot_at']-GRACE for s in sessions))

    def _closed(self, collection_id, now):
        with self.store.db() as db:
            sessions = [dict(r) for r in db.execute('SELECT * FROM automation_sessions WHERE collection_id=? ORDER BY started', (collection_id,))]
        if self._history_closed(collection_id, sessions):
            return True
        if not sessions or any(s['status'] == 'recording' or s['unfinished'] for s in sessions):
            return False
        monitor = self._monitor(now)
        latest = sessions[-1]
        return (monitor['fresh'] and monitor['live_state'] == 'offline'
                and monitor['douyin_id'] == latest['owner']
                and monitor['offline_since'] is not None and now - monitor['offline_since'] >= GRACE
                and latest['ended'] is not None and now - latest['ended'] >= GRACE)

    def _monitor(self, now):
        result = dict(fresh=False, live_state='unknown', douyin_id='', offline_since=None)
        path = self.settings.recordings / 'monitor.json'
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                return result
            body = json.loads(path.read_text(encoding='utf-8'))
            observed = body.get('observed_at')
            observed = float(observed) if isinstance(observed, (int, float)) else timestamp(observed)
            offline = body.get('offline_since')
            offline = float(offline) if isinstance(offline, (int, float)) else timestamp(offline)
            result.update(fresh=observed is not None and -60 <= now-observed <= 180,
                          live_state=body.get('live_state') if body.get('live_state') in {'offline','recording','unknown'} else 'unknown',
                          douyin_id=str(body.get('douyin_id') or ''),offline_since=offline)
        except (OSError, ValueError, TypeError):
            pass
        return result

    def record_revision(self, old_tid, new_tid, now=None):
        now = time.time() if now is None else float(now)
        body = self.store.transcript(new_tid)
        old = self.store.transcript(old_tid)
        if not body or not old or body.get('recording', {}).get('id') != old.get('recording', {}).get('id'):
            raise ValueError('文字版本不属于同一录音')
        quality=self.store.assess_content(body)
        state='no_speech' if quality['decision']=='exclude' else 'ready'
        with self.store.db(True) as db:
            rows = db.execute("SELECT asset_key,collection_id FROM automation_assets WHERE transcript_id=? AND state IN ('ready','no_speech')", (old_tid,)).fetchall()
            for row in rows:
                previous = db.execute('SELECT job_id FROM automation_generations WHERE collection_id=? ORDER BY created DESC LIMIT 1', (row['collection_id'],)).fetchone()
                db.execute('UPDATE automation_assets SET transcript_id=?,state=? WHERE asset_key=?', (new_tid,state,row['asset_key']))
                if self.daily and db.execute('SELECT 1 FROM automation_daily_assets WHERE asset_key=?',(row['asset_key'],)).fetchone():
                    continue  # Revised daily sources are picked up next 09:00.
                db.execute('INSERT OR REPLACE INTO automation_revisions VALUES(?,?,?)',
                           (row['collection_id'], now+60, previous[0] if previous else None))
        return bool(rows)

    def _sync_assets(self, now):
        # Previously completed ASR can predate the content filter. Recheck its
        # saved body without rerunning recognition or paying for a summary.
        with self.store.db() as db:
            completed=[dict(r) for r in db.execute("SELECT asset_key,transcript_id,state FROM automation_assets WHERE state IN ('ready','no_speech') AND transcript_id IS NOT NULL")]
        for asset in completed:
            body=self.store.transcript(asset['transcript_id'])
            if not body:continue
            quality=self.store.assess_content(body)
            state='no_speech' if quality['decision']=='exclude' else 'ready'
            if state!=asset['state']:
                with self.store.db(True) as db:
                    db.execute('UPDATE automation_assets SET state=? WHERE asset_key=?',(state,asset['asset_key']))
        with self.store.db() as db:
            rows = [dict(r) for r in db.execute("SELECT a.*,EXISTS(SELECT 1 FROM automation_history_assets h WHERE h.asset_key=a.asset_key) historical "
                                               "FROM automation_assets a WHERE state NOT IN ('baseline','no_speech','ready','cancelled')")]
        for asset in rows:
            if self.daily and not asset['historical'] and not asset['asr_job']:
                with self.store.db() as db:
                    if not db.execute('SELECT 1 FROM automation_daily_assets WHERE asset_key=?',(asset['asset_key'],)).fetchone():
                        continue
            job = self.store.job(asset['asr_job']) if asset['asr_job'] else None
            if job and job['state'] == 'completed':
                ids = job['result'].get('transcript_ids', [])
                recording = json.loads(asset['recording'])
                bodies = [self.store.transcript(tid) for tid in ids]
                body = next((body for body in bodies if body and
                             self._recording_version(body.get('recording', {})) == self._recording_version(recording)), None)
                # A reused multi-file task may finish a different recording first.
                # Missing output must not be silently marked as no speech.
                if not body:
                    with self.store.db(True) as db:
                        db.execute("UPDATE automation_assets SET state='failed',retry_count=3 WHERE asset_key=?", (asset['asset_key'],))
                    continue
                quality=self.store.assess_content(body)
                state='no_speech' if quality['decision']=='exclude' else 'ready'
                with self.store.db(True) as db:
                    db.execute('UPDATE automation_assets SET state=?,transcript_id=? WHERE asset_key=?', (state, body['id'], asset['asset_key']))
                continue
            if job and job['state'] == 'failed':
                retry = asset['retry_count']
                if retry >= 3:
                    with self.store.db(True) as db:
                        db.execute("UPDATE automation_assets SET state='failed' WHERE asset_key=?", (asset['asset_key'],))
                    continue
                if asset['retry_at'] is None:
                    with self.store.db(True) as db:
                        db.execute("UPDATE automation_assets SET state='retry_wait',retry_at=? WHERE asset_key=?", (now + (60, 300, 900)[retry], asset['asset_key']))
                elif now >= asset['retry_at']:
                    self.store.retry(job['id'])
                    with self.store.db(True) as db:
                        db.execute("UPDATE automation_assets SET state='queued',retry_count=retry_count+1,retry_at=NULL WHERE asset_key=?", (asset['asset_key'],))
                continue
            if job and job['state'] == 'cancelled':
                with self.store.db(True) as db:
                    db.execute("UPDATE automation_assets SET state='cancelled' WHERE asset_key=?", (asset['asset_key'],))
                continue
            if job:
                continue  # Cancellation is explicit; do not silently undo it.
            recording = json.loads(asset['recording'])
            if self.store.content_filtered(recording):
                with self.store.db(True) as db:
                    db.execute("UPDATE automation_assets SET state='no_speech' WHERE asset_key=?",(asset['asset_key'],))
                continue
            payload = dict(recordings=[recording], automatic=True, collection_id=asset['collection_id'], asset_key=asset['asset_key'])
            if asset['historical']:
                payload['history_backfill'] = True
            try:
                job = self.store.create_job('transcribe', payload)
            except ValueError:
                # Registry retains the asset even when the existing queue is full.
                return
            with self.store.db(True) as db:
                db.execute("UPDATE automation_assets SET asr_job=?,state='queued' WHERE asset_key=?", (job['id'], asset['asset_key']))

    def _resume_daily_budget(self, now):
        day = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()
        with self.store.db() as db:
            rows = db.execute("""SELECT j.id,j.error,j.updated,COALESCE(SUM(COALESCE(u.actual,u.reserved)),0) spent,
                COALESCE(SUM(u.state='uncertain'),0) uncertain
                FROM jobs j LEFT JOIN usage u ON u.job_id=j.id
                WHERE j.state='awaiting_budget' AND json_extract(j.payload,'$.automatic')=1 GROUP BY j.id""").fetchall()
        if self.store.spending()['committed'] >= self.settings.daily_budget:
            return
        for row in rows:
            if self.daily:
                job=self.store.job(row['id'])
                if job['payload'].get('history_backfill') and not self._legacy_dispatch_allowed(now):
                    continue
            prior_day = datetime.fromtimestamp(row['updated'], timezone(timedelta(hours=8))).date().isoformat()
            if (prior_day < day and not row['uncertain'] and row['spent'] < self.settings.task_budget
                    and row['error'].startswith('每日预算不足')):
                self.store.retry(row['id'])

    def _historical_business(self, transcript_ids):
        wanted = set(transcript_ids)
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE mode='business' ORDER BY created DESC,rowid DESC").fetchall()
        topic_prior = None
        for row in rows:
            job = self.store.unpack(row)
            ids = job['result'].get('transcript_ids') or job['payload'].get('transcript_ids', [])
            if set(ids) != wanted or job['payload'].get('business_version') != 'business-cases-v1':
                continue
            # In-flight, failed or cancelled jobs remain the same durable work:
            # history registration never circumvents their budget/cancellation.
            if not job['payload'].get('script_topic') and (job['state'] != 'completed' or job['payload'].get('all_cases')):
                return job
            if job['state'] == 'completed' and job['result'].get('dossier'):
                topic_prior = topic_prior or job
        return topic_prior

    def _dispatch_business(self, now):
        if self.daily and not self._legacy_dispatch_allowed(now):
            return
        with self.store.db() as db:
            collections = [dict(r) for r in db.execute('SELECT * FROM automation_collections ORDER BY started')]
        for collection in collections:
            cid = collection['id']
            with self.store.db() as db:
                assets = [dict(r) for r in db.execute("SELECT * FROM automation_assets WHERE collection_id=? AND state!='baseline' ORDER BY json_extract(recording,'$.path')", (cid,))]
                generations = [dict(r) for r in db.execute('SELECT * FROM automation_generations WHERE collection_id=? ORDER BY created DESC', (cid,))]
                revision = db.execute('SELECT * FROM automation_revisions WHERE collection_id=?', (cid,)).fetchone()
                historical = db.execute('SELECT 1 FROM automation_history_collections WHERE collection_id=?', (cid,)).fetchone()
                if self.daily:
                    daily_keys={r[0] for r in db.execute('SELECT asset_key FROM automation_daily_assets')}
                    # Daily batches have their own queue and do not inherit a
                    # permanently budget-blocked legacy generation.
                    assets=[a for a in assets if a['asset_key'] not in daily_keys]
                    assigned={key for g in generations for key in json.loads(g['asset_keys'])}
                    historical_keys={r[0] for r in db.execute('SELECT asset_key FROM automation_history_assets')}
                    assets=[a for a in assets if a['asset_key'] in assigned or a['asset_key'] in historical_keys]
            if not assets:
                continue
            closed = self._closed(cid, now)
            last = self.store.job(generations[0]['job_id']) if generations else None
            consumed = set(json.loads(generations[0]['asset_keys'])) if generations else set()
            ready = [a for a in assets if a['state'] == 'ready' and a['transcript_id']]
            new = [a for a in ready if a['asset_key'] not in consumed]
            settled = all(a['state'] in {'ready', 'no_speech'} for a in assets)
            state = 'completed' if closed and settled else 'partial' if closed else 'open'
            upgrade = bool(historical and last and last['state'] == 'completed' and not last['payload'].get('all_cases'))
            if historical and ready and (new or not last or last['state'] != 'completed' or upgrade or revision):
                state = 'partial' if closed else 'open'
            with self.store.db(True) as db:
                db.execute('UPDATE automation_collections SET state=?,updated=? WHERE id=?', (state, now, cid))
            if last and last['state'] != 'completed':
                continue
            if historical and any(a['state'] in {'observed', 'queued', 'retry_wait'} for a in assets):
                continue
            dirty = revision is not None and now >= revision['due_at']
            if not new and not dirty and not upgrade:
                continue
            if not ready:
                # Editing the last useful text into fillers must not schedule a
                # paid job with no usable source material.
                if dirty:
                    with self.store.db(True) as db:
                        db.execute('DELETE FROM automation_revisions WHERE collection_id=?',(cid,))
                continue
            if historical and not dirty:
                # Bound newly read material, retaining the prior complete context
                # through transcript_ids + previous_job_id. A failed batch keeps
                # its original identity/budget instead of being split to retry.
                batch, batch_ms, batch_bytes = [], 0, 0
                for asset in new:
                    body = self.store.transcript(asset['transcript_id']) or {}
                    length = len(packed(body.get('segments', [])).encode('utf-8'))
                    duration_ms = body.get('duration_ms', 0)
                    if batch and (batch_ms + duration_ms > HISTORY_BATCH_MS or batch_bytes + length > HISTORY_BATCH_BYTES):
                        break
                    batch.append(asset)
                    batch_ms += duration_ms
                    batch_bytes += length
                selected = consumed | {a['asset_key'] for a in batch}
                ready = [a for a in ready if a['asset_key'] in selected]
                new = batch
            duration = sum((self.store.transcript(a['transcript_id']) or {}).get('duration_ms', 0) for a in new)
            tail = closed and all(a['state'] not in {'observed', 'queued', 'retry_wait'} for a in assets)
            if not historical and not dirty and not tail and (duration < MIN_AUDIO_MS or (generations and now - generations[0]['created'] < UPDATE_SECONDS)):
                continue
            ids = [a['transcript_id'] for a in ready]
            keys = [a['asset_key'] for a in ready]
            input_key = digest(['history-business-all-v1' if historical else 'auto-business-v1', cid, ids])
            if historical and not generations:
                prior = self._historical_business(ids)
                if prior and not prior['payload'].get('script_topic') and (prior['state'] != 'completed' or prior['payload'].get('all_cases')):
                    with self.store.db(True) as db:
                        db.execute('INSERT OR IGNORE INTO automation_generations VALUES(?,?,?,?,?,?)',
                                   (input_key, cid, prior['id'], packed(keys), packed(ids), now))
                    continue
                if prior:
                    last = prior
            payload = dict(transcript_ids=ids, automatic=True, collection_id=cid,
                           generation_key=input_key, business_version='business-cases-v1',
                           coverage=dict(ready=len(ready), total=len(assets), final=closed and settled and len(ready)+sum(a['state']=='no_speech' for a in assets)==len(assets)))
            if historical:
                payload.update(all_cases=True, history_backfill=True)
            if last:
                payload['previous_job_id'] = last['id']
            if dirty and revision['billing_job_id']:
                payload['billing_group_id'] = revision['billing_job_id']
            try:
                with self.store.db() as db:
                    existing = db.execute("SELECT id FROM jobs WHERE mode='business' AND json_extract(payload,'$.generation_key')=?", (input_key,)).fetchone()
                job = self.store.job(existing[0]) if existing else self.store.create_job('business', payload)
            except ValueError:
                return
            with self.store.db(True) as db:
                db.execute('INSERT OR IGNORE INTO automation_generations VALUES(?,?,?,?,?,?)',
                           (input_key, cid, job['id'], packed(keys), packed(ids), now))
                if dirty:
                    db.execute('DELETE FROM automation_revisions WHERE collection_id=?', (cid,))

    def _legacy_dispatch_allowed(self, now):
        date=datetime.fromtimestamp(now,timezone(timedelta(hours=8)))
        if date.hour<self.settings.daily_send_hour:
            return False
        with self.store.db() as db:
            day=date.date().isoformat()
            if db.execute("SELECT 1 FROM jobs WHERE json_extract(payload,'$.daily_batch_day') IS NOT NULL AND (state IN ('queued','running') OR (state='awaiting_budget' AND (json_extract(payload,'$.daily_batch_day')=? OR error LIKE '每日预算不足%'))) LIMIT 1",(day,)).fetchone():
                return False
            if db.execute("SELECT 1 FROM automation_daily_assets d JOIN automation_assets a USING(asset_key) WHERE d.day<=? AND a.state IN ('observed','queued','retry_wait') LIMIT 1",(day,)).fetchone():
                return False
            if db.execute("SELECT 1 FROM automation_daily_assets d JOIN automation_assets a USING(asset_key) WHERE d.day<=? AND a.state='ready' AND NOT EXISTS(SELECT 1 FROM automation_daily_generations g WHERE g.day=d.day AND g.collection_id=a.collection_id) LIMIT 1",(day,)).fetchone():
                return False
        return True

    def _snapshot_daily(self, batch, recordings, now):
        """Freeze only finalized files at the scheduled cutoff; never reset jobs."""
        cutoff=batch['cutoff'];day=batch['day']
        eligible=[r for r in recordings if int(r.get('mtime_ns') or 0)/1e9 <= cutoff]
        bodies={}
        with self.store.db() as db:
            for row in db.execute('SELECT body FROM transcripts ORDER BY created DESC,rowid DESC'):
                body=json.loads(row['body'])
                if body.get('status') in {'ready','no_speech'}:
                    bodies.setdefault(self._recording_version(body.get('recording',{})),body)
        qualities={b['id']:self.store.assess_content(b) for b in bodies.values()}
        with self.store.db(True) as db:
            assigned={k for r in db.execute('SELECT asset_keys FROM automation_generations') for k in json.loads(r[0])}
            historical={r[0] for r in db.execute('SELECT asset_key FROM automation_history_assets')}
            previous_jobs={}
            for row in db.execute("SELECT id,payload,state,stage FROM jobs WHERE state!='completed' ORDER BY created DESC"):
                for r in json.loads(row['payload']).get('recordings',[]):
                    previous_jobs.setdefault(self._recording_version(r),dict(row))
            for recording in eligible:
                key=digest([recording['id'],recording['size'],str(recording['mtime_ns'])])
                if key in assigned or key in historical:
                    continue
                asset=db.execute('SELECT * FROM automation_assets WHERE asset_key=?',(key,)).fetchone()
                if not asset:
                    continue
                added=db.execute('INSERT OR IGNORE INTO automation_daily_assets VALUES(?,?)',(key,day)).rowcount
                if not added or asset['asr_job'] or asset['state'] not in {'baseline','observed'}:
                    continue
                body=bodies.get(self._recording_version(recording))
                prior=previous_jobs.get(self._recording_version(recording))
                if body:
                    state='no_speech' if qualities[body['id']]['decision']=='exclude' else 'ready'
                    db.execute('UPDATE automation_assets SET state=?,transcript_id=? WHERE asset_key=?',(state,body['id'],key))
                elif prior and prior['state']=='cancelled':
                    db.execute("UPDATE automation_assets SET state='cancelled',asr_job=? WHERE asset_key=?",(prior['id'],key))
                elif prior and prior['stage']=='transcribing' and prior['state'] in {'queued','running'}:
                    db.execute("UPDATE automation_assets SET state='queued',asr_job=? WHERE asset_key=?",(prior['id'],key))
                else:
                    db.execute("UPDATE automation_assets SET state='observed' WHERE asset_key=?",(key,))
            db.execute("UPDATE automation_daily_batches SET state='registered' WHERE day=?",(day,))

    def _dispatch_daily(self, now):
        """One analysis per collection/day, independent of blocked old history."""
        with self.store.db() as db:
            groups=list(db.execute('SELECT DISTINCT d.day,a.collection_id FROM automation_daily_assets d JOIN automation_assets a USING(asset_key) ORDER BY d.day,a.collection_id'))
        for day,cid in groups:
            with self.store.db() as db:
                existing=db.execute('SELECT * FROM automation_daily_generations WHERE day=? AND collection_id=?',(day,cid)).fetchone()
                assets=[dict(r) for r in db.execute('SELECT a.* FROM automation_daily_assets d JOIN automation_assets a USING(asset_key) WHERE d.day=? AND a.collection_id=? ORDER BY json_extract(a.recording,\'$.path\')',(day,cid))]
                previous=db.execute("SELECT j.* FROM automation_daily_generations g JOIN jobs j ON j.id=g.job_id WHERE g.collection_id=? AND g.day<? AND j.state='completed' ORDER BY g.day DESC LIMIT 1",(cid,day)).fetchone()
                earlier=db.execute("SELECT 1 FROM automation_daily_generations g JOIN jobs j ON j.id=g.job_id WHERE g.collection_id=? AND g.day<? AND j.state IN ('queued','running')",(cid,day)).fetchone()
            if existing:
                today=datetime.fromtimestamp(now,timezone(timedelta(hours=8)))
                # A terminal ASR failure may be manually repaired days later.
                # Move only newly recovered, previously unconsumed sources into
                # a later daily batch; never rerun the earlier paid job.
                if today.date().isoformat()>day and today.hour>=self.settings.daily_process_hour:
                    consumed=set(json.loads(existing['asset_keys']))
                    consumed_ids=set(json.loads(existing['transcript_ids']))
                    with self.store.db(True) as db:
                        for a in assets:
                            if a['state']=='ready' and (a['asset_key'] not in consumed or a['transcript_id'] not in consumed_ids):
                                target=today.date().isoformat()
                                if db.execute('SELECT 1 FROM automation_daily_generations WHERE day=? AND collection_id=?',(target,cid)).fetchone():
                                    target=(today.date()+timedelta(days=1)).isoformat()
                                db.execute('UPDATE automation_daily_assets SET day=? WHERE asset_key=?',(target,a['asset_key']))
                continue
            date=datetime.fromtimestamp(now,timezone(timedelta(hours=8)))
            if day>date.date().isoformat() or (day==date.date().isoformat() and date.hour<self.settings.daily_process_hour):
                continue
            if earlier or any(a['state'] in {'baseline','observed','queued','retry_wait'} for a in assets):
                continue
            ready=[a for a in assets if a['state']=='ready' and a['transcript_id']]
            if not ready:
                # Failed/cancelled inputs remain visible and can be retried;
                # do not mark a zero-material analysis as completed.
                continue
            prior=self.store.unpack(previous) if previous else None
            ids=[a['transcript_id'] for a in ready]
            if prior:
                replaced={a['recording_id'] for a in ready}
                old_ids=[tid for tid in prior['result'].get('transcript_ids',[])
                         if (self.store.transcript(tid) or {}).get('recording',{}).get('id') not in replaced]
                ids=list(dict.fromkeys(old_ids+ids))
            key=digest(['daily-business-v1',day,cid,ids])
            from .narration import NARRATION_VERSION
            payload=dict(transcript_ids=ids,automatic=True,collection_id=cid,generation_key=key,
                         business_version='business-cases-v1',narration_version=NARRATION_VERSION,
                         daily_batch_day=day,all_cases=True,
                         coverage=dict(ready=len(ready),total=len(assets),final=True))
            if prior:payload['previous_job_id']=prior['id']
            try:
                reusable=self._historical_business(ids)
                job=(reusable if reusable and not reusable['payload'].get('script_topic')
                     and (reusable['state']!='completed' or reusable['payload'].get('all_cases'))
                     else self.store.create_job('business',payload))
            except ValueError:
                return
            with self.store.db(True) as db:
                db.execute('INSERT OR IGNORE INTO automation_daily_generations VALUES(?,?,?,?,?,?)',
                           (day,cid,job['id'],packed([a['asset_key'] for a in ready]),packed(ids),now))

    def tick(self, now=None):
        now = time.time() if now is None else float(now)
        try:
            sessions = self._manifests()
            recordings = scan(self.settings.recordings, include_duration=True)
            with self.store.db() as db:
                config = dict(db.execute('SELECT * FROM automation_config WHERE id=1').fetchone())
            # Disabled mode still registers no work; the first enable snapshots
            # current completed files instead of unexpectedly replaying history.
            if not config['enabled']:
                if self.daily:
                    from .case_library import list_cases
                    self.daily.tick(now,list_cases(self.store))
                return self.status()
            self._register(sessions, recordings, now)
            if self.daily:
                from .case_library import list_cases
                self.daily.initialize(now,list_cases(self.store))
                batch=self.daily.snapshot_due(now)
                if batch:self._snapshot_daily(batch,recordings,now)
            self._sync_assets(now)
            self._resume_daily_budget(now)
            self._dispatch_business(now)
            if self.daily:
                self._dispatch_daily(now)
                self.daily.tick(now,list_cases(self.store),pending=self.status()['pending'])
        except Exception as exc:
            # Keep errors credential/content-free, including corrupt manifests.
            with self.store.db(True) as db:
                db.execute('UPDATE automation_config SET last_scan=?,last_error=? WHERE id=1', (now, '自动调度异常：'+type(exc).__name__))
            LOG.exception('automatic scheduler tick failed')
            raise
        return self.status()

    def status(self):
        with self.store.db() as db:
            config = dict(db.execute('SELECT * FROM automation_config WHERE id=1').fetchone())
            assets = {r['state']: r['n'] for r in db.execute('SELECT state,count(*) n FROM automation_assets GROUP BY state')}
            collections = []
            for row in db.execute('SELECT * FROM automation_collections ORDER BY started DESC LIMIT 30'):
                item = dict(row)
                item.update(dict(db.execute("SELECT count(*) total,SUM(state='ready') ready,SUM(state='no_speech') no_speech,SUM(state='failed') failed FROM automation_assets WHERE collection_id=? AND state!='baseline'", (row['id'],)).fetchone()))
                if not item['total']:
                    continue
                latest = db.execute('SELECT g.job_id,j.state,j.error FROM automation_generations g JOIN jobs j ON j.id=g.job_id WHERE g.collection_id=? ORDER BY g.created DESC LIMIT 1', (row['id'],)).fetchone()
                if latest:
                    item.update(job_id=latest['job_id'], job_state=latest['state'], error=latest['error'])
                collections.append(item)
            history_states = {r['state']: r['n'] for r in db.execute(
                'SELECT a.state,count(*) n FROM automation_history_assets h JOIN automation_assets a USING(asset_key) GROUP BY a.state')}
            history_reuse = dict(db.execute('SELECT count(*) total,COALESCE(SUM(reused_transcript),0) reused_transcripts,'
                                          'COALESCE(SUM(reused_asr),0) reused_asr FROM automation_history_assets').fetchone())
            covered = set()
            history_jobs = {'queued': 0, 'running': 0, 'completed': 0, 'failed': 0, 'cancelled': 0, 'awaiting_budget': 0}
            for generation in db.execute("SELECT g.asset_keys,j.state,COALESCE(json_extract(j.payload,'$.all_cases'),0) all_cases FROM automation_generations g "
                                         'JOIN jobs j ON j.id=g.job_id WHERE g.collection_id IN '
                                         '(SELECT collection_id FROM automation_history_collections)'):
                history_jobs[generation['state']] = history_jobs.get(generation['state'], 0) + 1
                if generation['state'] == 'completed' and generation['all_cases']:
                    covered.update(json.loads(generation['asset_keys']))
            history_keys = {r[0] for r in db.execute('SELECT asset_key FROM automation_history_assets')}
            generated = len(covered & history_keys)
            no_speech = history_states.get('no_speech', 0)
            history = dict(history_reuse, states=history_states, jobs=history_jobs,
                           analyzed=generated, generated=generated,
                           transcribed=history_states.get('ready', 0)+no_speech,
                           pending=max(0,history_reuse['total']-generated-no_speech),
                           failed=history_states.get('failed',0), cancelled=history_states.get('cancelled',0),
                           failed_jobs=history_jobs.get('failed',0),
                           waiting_budget=history_jobs.get('awaiting_budget',0))
            waiting = db.execute("SELECT count(*) FROM jobs WHERE state='awaiting_budget' AND json_extract(payload,'$.automatic')=1").fetchone()[0]
        result = dict(enabled=bool(config['enabled']), activated_at=config['activated_at'], last_scan=config['last_scan'],
                    last_error=config['last_error'], pending=assets.get('observed', 0),
                    transcribing=assets.get('queued', 0) + assets.get('retry_wait', 0),
                    failed=assets.get('failed', 0), waiting_budget=waiting, collections=collections,
                    recorder=self._monitor(time.time()), history=history)
        if self.daily:result['daily']=self.daily.status()
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--backfill-history', action='store_true', help='Register all current valid history; idempotent, respects pause and budgets')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    settings = Settings.load()
    scheduler = Automation(settings, Store(settings.data))
    if args.backfill_history:
        print(json.dumps(scheduler.backfill_history(), ensure_ascii=False))
        return
    while not stop.is_set():
        try:
            scheduler.tick()
        except Exception:
            LOG.error('tick stopped; durable registrations retained')
        if args.once:
            break
        stop.wait(60)


if __name__ == '__main__':
    main()
