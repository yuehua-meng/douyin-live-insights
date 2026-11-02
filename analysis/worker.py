import argparse
import logging
import signal
import threading
import time
import traceback

from .settings import Settings
from .store import Store, Cancelled, BudgetExceeded
from .asr import Engine, transcribe
from .summarize import Summarizer

STOP = threading.Event()
LOG = logging.getLogger('analysis-worker')


def process(stage, settings, store, claim, engine_loader):
    job_id, owner = claim
    done = threading.Event()
    lost = threading.Event()

    def check():
        if STOP.is_set() or lost.is_set():
            raise Cancelled()
        store.heartbeat(job_id, owner)

    def pulse():
        while not done.wait(15):
            try:
                store.heartbeat(job_id, owner)
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        job = store.job(job_id)
        result = job['result']
        if stage == 'transcribing':
            ids = []
            usable_ids = []
            excluded = []
            recordings = job['payload']['recordings']
            for i, recording in enumerate(recordings):
                check()
                def progress(done_count, total):
                    store.update(job_id, owner, progress=dict(file=i+1, files=len(recordings), done=done_count, total=total))
                body = transcribe(recording, settings, store, engine_loader, check, progress)
                ids.append(body['id'])
                quality = store.assess_content(body)
                if quality['decision'] == 'exclude':
                    excluded.append(dict(id=body['id'],recording_id=recording['id'],reason=quality['reason']))
                else:
                    usable_ids.append(body['id'])
                # ASR-only jobs retain all output IDs so the durable scheduler
                # can resolve the exact recording and explain an exclusion.
                result = dict(transcript_ids=ids, usable_transcript_ids=usable_ids,
                              excluded_transcripts=excluded, selection=recordings)
                store.update(job_id, owner, result=result)
            check()
            if job['mode'] in {'both','business'} and usable_ids:
                store.update(job_id, owner, stage='summarizing', state='queued', progress={})
            else:
                if not usable_ids:
                    result.update(content_filtered=True,filter_reason='没有可用于总结的有效转写内容')
                store.update(job_id, owner, state='completed', result=result, error='')
        else:
            ids = result.get('source_transcript_ids') or result.get('transcript_ids') or job['payload'].get('transcript_ids', [])
            transcripts = [store.transcript(t) for t in ids]
            if not transcripts or any(t is None for t in transcripts):
                raise ValueError('转写版本不存在')
            usable, excluded = [], []
            for body in transcripts:
                quality=store.assess_content(body)
                if quality['decision']=='exclude':
                    excluded.append(dict(id=body['id'],recording_id=body.get('recording',{}).get('id'),reason=quality['reason']))
                else:
                    usable.append(body)
            filtered_ids=[t['id'] for t in usable]
            # A retry may carry a summary/dossier created before the filter was
            # introduced. Never reuse derived content containing removed input.
            previous_ids=result.get('transcript_ids') or ids
            if excluded and (set(previous_ids)!=set(filtered_ids) or not usable):
                for key in ('summary','script','dossier','display_title','naming_version'):
                    result.pop(key,None)
            result.update(source_transcript_ids=ids,transcript_ids=filtered_ids,excluded_transcripts=excluded)
            result.pop('content_filtered',None);result.pop('filter_reason',None)
            if not usable:
                result.update(content_filtered=True,filter_reason='没有可用于总结的有效转写内容')
                check()
                store.update(job_id,owner,state='completed',result=result,error='')
                return
            transcripts=usable
            store.update(job_id, owner, result=result)
            progress = lambda count, total: store.update(job_id, owner, progress=dict(done=count, total=total))
            if job['mode'] == 'business':
                from .business import run
                prior=store.job(job['payload'].get('previous_job_id',''))
                if prior and set(prior['result'].get('transcript_ids',[]))-set(filtered_ids):
                    prior=None
                def checkpoint(partial):
                    check()
                    result.update(partial)
                    store.update(job_id,owner,result=result)
                output=run(settings,store,job_id,transcripts,check,progress,
                           previous=prior['result'] if prior else None,on_result=checkpoint)
            elif job['mode'] == 'script':
                from .script import generate
                output = {'script': generate(settings, store, job_id, transcripts, check, progress, topic=job['payload'].get('script_topic', ''))}
            else:
                output = {'summary': Summarizer(settings, store, job_id, check).run(transcripts, progress)}
            check()
            store.update(job_id, owner, state='completed', result=dict(result, **output), error='')
    except Cancelled:
        try:
            job = store.job(job_id)
            store.update(job_id, owner, state='cancelled' if job['cancel'] else 'queued', error='任务已停止，可继续处理')
        except Cancelled:
            pass
    except BudgetExceeded as exc:
        store.update(job_id, owner, state='awaiting_budget', error=str(exc))
    except Exception as exc:
        # Never include HTTP response bodies or credential-bearing exception text.
        safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else f'处理失败：{type(exc).__name__}'
        store.update(job_id, owner, state='failed', error=safe[:300])
        # Keep actionable locations without logging exception bodies, source
        # lines, credentials or user transcript contents.
        frames = ' > '.join(f'{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(exc.__traceback__))
        LOG.error('job=%s failed type=%s locations=%s', job_id, type(exc).__name__, frames)
    finally:
        done.set()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['transcribing', 'summarizing'])
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: STOP.set())
    settings = Settings.load()
    store = Store(settings.data)
    engine = None
    def loader():
        nonlocal engine
        if engine is None:
            engine = Engine(settings)
        return engine
    while not STOP.is_set():
        claim = store.claim(args.stage)
        if claim:
            process(args.stage, settings, store, claim, loader)
        elif args.once:
            return
        else:
            STOP.wait(2)
        if args.once:
            return


if __name__ == '__main__':
    main()
