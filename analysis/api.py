"""Authenticated analysis dashboard. The public recorder stays read-only."""
import hashlib
import hmac
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import BaseModel, Field

from .settings import Settings
from .store import Store
from .catalog import scan, safe_file
from .asr import export_transcript
from .summarize import markdown
from audio_quality import counts


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ':' + value


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class NewJob(BaseModel):
    mode: str = 'both'
    script_topic: str = Field(default='', max_length=200)
    recording_ids: list[str] = Field(default_factory=list, max_length=400)
    transcript_ids: list[str] = Field(default_factory=list, max_length=400)


class Switch(BaseModel):
    enabled: bool


class Retain(BaseModel):
    locked: bool = False


class Edit(BaseModel):
    texts: list[str] = Field(max_length=10000)


def create_app(settings=None):
    s = settings or Settings.load()
    store = Store(s.data)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    static = Path(__file__).parent / 'static'

    @app.middleware('http')
    async def security(request, call_next):
        # Bound streamed as well as Content-Length request bodies before parsing JSON.
        if request.method in {'POST', 'PUT', 'PATCH'}:
            if request.headers.get('origin') != s.origin:
                return JSONResponse({'detail': '请求来源不匹配，请从管理页面操作'}, 403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2_000_000:
                    return JSONResponse({'detail': '请求内容过大'}, 413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def auth(request: Request):
        token = hashlib.sha256(request.cookies.get('analysis_session', '').encode()).hexdigest()
        with store.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE token_hash=? AND expires>?', (token, time.time())).fetchone()
        if not row:
            raise HTTPException(401, '请先登录')
        if request.method != 'GET' and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf']):
            raise HTTPException(403, '登录校验失效，请刷新页面')
        return dict(row)

    def job_or_404(job_id):
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, '任务不存在')
        return dict(job,locked=store.locked(job_id))

    def transcript_or_404(tid):
        body = store.transcript(tid)
        if not body:
            raise HTTPException(404, '转写不存在')
        return body

    @app.get('/api/transcripts/{tid}/versions', dependencies=[Depends(auth)])
    def versions(tid: str):
        original = transcript_or_404(tid)
        import json
        with store.db() as db:
            rows = db.execute("SELECT body FROM transcripts WHERE json_extract(body,'$.recording.id')=? ORDER BY created DESC",
                              (original['recording']['id'],)).fetchall()
        return [{'id': b['id'], 'revision': b['revision']} for b in (json.loads(r[0]) for r in rows)]

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({'detail': str(exc)[:300]}, 400)

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    @app.get('/app.js')
    def js():
        return FileResponse(static / 'app.js', media_type='text/javascript')

    @app.get('/style.css')
    def css():
        return FileResponse(static / 'style.css', media_type='text/css')

    @app.post('/api/login')
    def login(body: Login, request: Request):
        if not s.password_hash:
            raise HTTPException(503, '管理员密码尚未配置')
        # All requests pass through one proxy: deliberately global, not spoofable XFF.
        with store.db(True) as db:
            db.execute('DELETE FROM login_attempts WHERE created<?', (time.time()-300,))
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            if db.execute('SELECT count(*) FROM login_attempts').fetchone()[0] >= 10:
                raise HTTPException(429, '登录尝试过多，请 5 分钟后再试')
            db.execute('INSERT INTO login_attempts VALUES(?,?)', ('admin', time.time()))
        salt = s.password_hash.split(':')[0]
        if not hmac.compare_digest(password_hash(body.password, salt), s.password_hash):
            raise HTTPException(401, '密码不正确')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.db(True) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), csrf, time.time()+28800))
        response = JSONResponse({'csrf': csrf})
        response.set_cookie('analysis_session', token, max_age=28800, httponly=True,
                            secure=s.secure_cookie, samesite='strict', path=s.cookie_path)
        return response

    @app.get('/api/auth')
    def me(session=Depends(auth)):
        return {'csrf': session['csrf']}

    @app.post('/api/logout')
    def logout(session=Depends(auth)):
        with store.db(True) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (session['token_hash'],))
        response = JSONResponse({'ok': True})
        response.delete_cookie('analysis_session', path=s.cookie_path)
        return response

    @app.get('/api/status', dependencies=[Depends(auth)])
    def status():
        return {'model': s.deepseek_model, 'asr': s.model_version,
                'daily_budget': s.daily_budget, 'task_budget': s.task_budget, 'usage': store.spending(),
                'models_ready': all((Path(p)/'model_quant.onnx').is_file() for p in (s.asr_model, s.vad_model, s.punc_model)),
                'filtering': dict(counts(s.recordings),content=store.content_filter_counts())}

    @app.get('/api/recordings', dependencies=[Depends(auth)])
    def recordings():
        completed = {(r['id'], r['size'], r['mtime_ns']) for r in store.transcribed_recordings()}
        excluded=store.content_filter_keys()
        return [dict(r, transcribed=(r['id'], r['size'], str(r['mtime_ns'])) in completed,
                     recording_version=str(r['mtime_ns'])) for r in scan(s.recordings, include_duration=True)
                     if store.content_version(r) not in excluded]

    @app.get('/api/recordings/transcribed', dependencies=[Depends(auth)])
    def transcribed_recordings():
        return store.transcribed_recordings()

    @app.get('/api/jobs', dependencies=[Depends(auth)])
    def jobs():
        return store.job_headers()

    @app.get('/api/results', dependencies=[Depends(auth)])
    def result_catalog():
        return store.result_catalog()

    @app.get('/api/cases', dependencies=[Depends(auth)])
    def case_library():
        from .case_library import list_cases
        return list_cases(store)

    @app.get('/api/cases/{case_id}', dependencies=[Depends(auth)])
    def case_detail(case_id: str):
        from .case_library import get_case
        result = get_case(store, case_id)
        if result is None:
            raise HTTPException(404, '这篇口播稿已更新或不存在，请刷新列表')
        return result

    @app.get('/api/projects', dependencies=[Depends(auth)])
    def project_catalog():
        return store.project_catalog()

    @app.get('/api/automation', dependencies=[Depends(auth)])
    def automation_status():
        from .automation import Automation
        return Automation(s,store).status()

    @app.post('/api/automation', dependencies=[Depends(auth)])
    def automation_switch(body: Switch):
        from .automation import Automation
        controller=Automation(s,store)
        controller.set_enabled(body.enabled)
        return controller.status()

    @app.get('/api/feed', dependencies=[Depends(auth)])
    def feed():
        auto=automation_status()
        groups={c.get('id',c.get('collection_id')):c for c in auto.get('collections',[])}
        entries=store.feed()
        for entry in entries:
            group=groups.get(entry['collection_id'])
            if group:
                # ASR may already cover the entire historical collection while
                # business processing is still on an earlier incremental batch.
                entry['coverage']={'done':min(len(entry['transcript_ids'])+int(group.get('no_speech') or 0),int(group.get('total') or 0)),
                                   'total':int(group.get('total') or 0),'failed':int(group.get('failed') or 0)}
                entry['partial']=group.get('state')!='completed' or entry['state']!='completed' or entry['coverage']['done']<entry['coverage']['total']
        return {'entries':entries,'automation':auto}

    @app.post('/api/jobs', dependencies=[Depends(auth)])
    def new_job(body: NewJob):
        if (s.data/'filter-maintenance.lock').exists():
            raise HTTPException(503, '正在清理历史无效录音，请稍后提交；已有任务继续运行')
        if body.mode not in {'transcribe', 'both', 'summary', 'script','business'}:
            raise ValueError('任务类型无效')
        if body.recording_ids and body.transcript_ids:
            raise ValueError('请选择录音或文字版本中的一种输入')
        if body.mode not in {'business','both'} and max(len(body.recording_ids),len(body.transcript_ids))>40:
            raise ValueError('单独处理每次最多 40 个片段')
        if body.mode in {'summary', 'script'} or (body.mode=='business' and body.transcript_ids):
            ids = list(dict.fromkeys(body.transcript_ids))
            if not ids:
                raise ValueError('请先选择转写结果')
            for tid in ids:
                transcript_or_404(tid)
            payload = {'transcript_ids': ids}
            if body.mode == 'script':
                from .script import VERSION
                from .narration import NARRATION_VERSION
                payload.update(script_version=VERSION, narration_version=NARRATION_VERSION,
                               script_topic=body.script_topic.strip())
        else:
            ids = list(dict.fromkeys(body.recording_ids))
            if not ids:
                raise ValueError('请先选择录音')
            excluded=store.content_filter_keys()
            catalog = {r['id']: r for r in scan(s.recordings) if store.content_version(r) not in excluded}
            if any(rid not in catalog for rid in ids):
                raise ValueError('录音已被过滤、不存在或尚未完成，请刷新录音列表')
            selected = sorted((catalog[rid] for rid in ids), key=lambda r: r['path'])
            payload = {'recordings': selected}
        mode='business' if body.mode in {'both','business'} else body.mode
        if mode=='business':
            from .narration import NARRATION_VERSION
            payload.update(business_version='business-cases-v1',narration_version=NARRATION_VERSION,
                           script_topic=body.script_topic.strip())
        return store.create_job(mode, payload)

    @app.get('/api/jobs/{job_id}', dependencies=[Depends(auth)])
    def get_job(job_id: str):
        return job_or_404(job_id)

    @app.post('/api/jobs/{job_id}/{action}', dependencies=[Depends(auth)])
    def action(job_id: str, action: str, body: Retain | None = None):
        current=job_or_404(job_id)
        if action == 'cancel':
            store.cancel(job_id)
        elif action == 'retry':
            if (s.data/'filter-maintenance.lock').exists():
                raise HTTPException(503, '正在清理历史无效录音，请稍后重试')
            store.retry(job_id)
        elif action=='lock':
            if not current['result'].get('script'):
                raise ValueError('口播生成后才能保留此稿')
            store.lock_output(job_id,body.locked if body else True)
        else:
            raise HTTPException(404)
        return job_or_404(job_id)

    @app.get('/api/transcripts/{tid}', dependencies=[Depends(auth)])
    def transcript(tid: str):
        return transcript_or_404(tid)

    @app.post('/api/transcripts/{tid}/revision', dependencies=[Depends(auth)])
    def revision(tid: str, edit: Edit):
        original = transcript_or_404(tid)
        if len(edit.texts) != len(original['segments']) or any(len(t)>10000 for t in edit.texts):
            raise ValueError('段落数不匹配或文字过长')
        new = dict(original, id=uuid.uuid4().hex, parent=tid, revision=original['revision']+1)
        new['segments'] = [dict(seg, id=f"{new['id']}:{i}", text=text.strip()) for i,(seg,text) in enumerate(zip(original['segments'],edit.texts))]
        store.save_transcript(new, parent=tid)
        store.assess_content(new)
        from .automation import Automation
        controller=Automation(s,store)
        if hasattr(controller,'record_revision'):
            controller.record_revision(tid,new['id'])
        return new

    @app.get('/api/transcripts/{tid}/export/{kind}', dependencies=[Depends(auth)])
    def export(tid: str, kind: str):
        if kind not in {'txt', 'srt', 'json'}:
            raise HTTPException(404)
        return Response(export_transcript(transcript_or_404(tid), kind), media_type='text/plain; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="transcript-{tid[:12]}.{kind}"'})

    @app.get('/api/jobs/{job_id}/summary.md', dependencies=[Depends(auth)])
    def export_summary(job_id: str):
        summary = job_or_404(job_id)['result'].get('summary')
        if not summary:
            raise HTTPException(404, '总结尚未生成')
        return Response(markdown(summary), media_type='text/markdown; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="summary-{job_id[:12]}.md"'})

    @app.get('/api/jobs/{job_id}/script.txt', dependencies=[Depends(auth)])
    def export_script(job_id: str):
        from .script import script_text
        script = job_or_404(job_id)['result'].get('script')
        if not script:
            raise HTTPException(404, '口播稿尚未生成')
        return Response(script_text(script) or script['reason'], media_type='text/plain; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="script-{job_id[:12]}.txt"'})

    @app.get('/api/media/{rid}', dependencies=[Depends(auth)])
    def media(rid: str, download: bool = False):
        recording = next((r for r in scan(s.recordings) if r['id']==rid), None)
        if not recording:
            raise HTTPException(404)
        return FileResponse(safe_file(s.recordings, recording), media_type='audio/mp4',
                            filename=recording['name'] if download else None)

    return app
