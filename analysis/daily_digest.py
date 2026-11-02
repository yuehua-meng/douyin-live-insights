"""Durable daily snapshots and a bounded, idempotent WeCom outbox.

No model calls here. Unknown HTTP outcomes require review rather than resend.
"""
from datetime import datetime, timedelta, timezone
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from .store import digest, packed

BEIJING = timezone(timedelta(hours=8))
MAX_BYTES = 4096


def local_day(now):
    return datetime.fromtimestamp(now, BEIJING)


def valid_webhook(value):
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, strict_parsing=True)
        return (url.scheme == 'https' and url.netloc == 'qyapi.weixin.qq.com'
                and url.path == '/cgi-bin/webhook/send' and not url.fragment
                and set(query) == {'key'} and len(query['key']) == 1
                and bool(re.fullmatch(r'[A-Za-z0-9_-]{10,200}', query['key'][0])))
    except (ValueError, TypeError):
        return False


def clean_text(value, limit):
    # Do not let model-derived titles inject markdown links, mentions or HTML.
    return re.sub(r'[\[\]<>`*_#@\\()]', '', str(value or '')).replace('\n', ' ').replace('\r', ' ')[:limit].strip()


def build_messages(day, cases, origin, pending=0):
    base = origin.rstrip('/')+'/recorder-admin/'
    url = urlsplit(base)
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError('日报页面地址必须使用有效 HTTPS 地址')
    intro = f'### {day} 商业案例日报\n今日新增或更新 {len(cases)} 篇口播稿。\n'
    footer = f'\n[打开全部口播稿与商业总结]({base})\n全文需登录查看；未完成录音保留处理进度，成果完成后纳入后续日报。'
    messages, text, versions = [], intro, []
    for case in cases:
        # IDs are server-generated hashes; never use arbitrary URL fragments.
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', str(case['id'])):
            continue
        title = clean_text(case['title'], 55) or '商业项目口播'
        stage = clean_text(case.get('stage'), 16)
        excerpt = clean_text(case.get('excerpt'), 75)
        block = f'\n**[{title}]({base}#case={case["id"]})**\n'+(f'{stage} · ' if stage else '')+excerpt+'\n'
        if versions and len((text+block+footer).encode('utf-8')) > MAX_BYTES:
            messages.append(dict(content=text+footer, versions=versions))
            text, versions = intro+'（续）\n', []
        if len((text+block+footer).encode('utf-8')) > MAX_BYTES:
            raise ValueError('单篇日报摘要超过消息长度')
        text += block
        versions.append([case['id'], case['version']])
    if versions or not messages:
        if not cases:
            text += '\n暂无新口播稿，已生成的历史稿件仍可在网页查看。\n'
        messages.append(dict(content=text+footer, versions=versions))
    if any(len(m['content'].encode('utf-8')) > MAX_BYTES for m in messages):
        raise ValueError('日报超过消息长度')
    return messages


class DailyDigest:
    def __init__(self, settings, store, transport=None):
        self.settings, self.store, self.transport = settings, store, transport
        if not (0 <= settings.daily_process_hour < settings.daily_send_hour <= 23):
            raise ValueError('每日处理时间必须早于发送时间')
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS daily_config(
                    id INTEGER PRIMARY KEY CHECK(id=1),activated REAL NOT NULL,
                    first_processing_day TEXT NOT NULL,first_send_day TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS automation_daily_batches(
                    day TEXT PRIMARY KEY,cutoff REAL NOT NULL,created REAL NOT NULL,state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS automation_daily_assets(
                    asset_key TEXT PRIMARY KEY,day TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS digest_seen(
                    case_id TEXT NOT NULL,version TEXT NOT NULL,source TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(case_id,version));
                CREATE TABLE IF NOT EXISTS digest_days(day TEXT PRIMARY KEY,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS digest_messages(
                    id TEXT PRIMARY KEY,day TEXT NOT NULL,ordinal INTEGER NOT NULL,
                    content TEXT NOT NULL,versions TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'ready',
                    attempts INTEGER NOT NULL DEFAULT 0,lease REAL,retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',sent_at REAL,UNIQUE(day,ordinal));
            ''')

    def initialize(self, now, cases):
        date = local_day(now)
        process_day = date.date()+timedelta(days=int(date.hour >= self.settings.daily_process_hour))
        send_day = date.date()+timedelta(days=int(date.hour >= self.settings.daily_send_hour))
        with self.store.db(True) as db:
            if db.execute('SELECT 1 FROM daily_config').fetchone():
                return
            db.execute('INSERT INTO daily_config VALUES(1,?,?,?)', (now, process_day.isoformat(), send_day.isoformat()))
            for case in cases:
                db.execute('INSERT OR IGNORE INTO digest_seen VALUES(?,?,?,?)', (case['id'],case['version'],'baseline',now))

    def snapshot_due(self, now):
        date = local_day(now);day=date.date().isoformat()
        with self.store.db(True) as db:
            config=db.execute('SELECT * FROM daily_config').fetchone()
            if not config or day < config['first_processing_day'] or date.hour < self.settings.daily_process_hour:
                return None
            cutoff=date.replace(hour=self.settings.daily_process_hour,minute=0,second=0,microsecond=0).timestamp()
            db.execute("INSERT OR IGNORE INTO automation_daily_batches VALUES(?,?,?,'collecting')",(day,cutoff,now))
            row=db.execute('SELECT * FROM automation_daily_batches WHERE day=?',(day,)).fetchone()
            return dict(row) if row['state']=='collecting' else None

    def unseen(self, cases):
        with self.store.db() as db:
            seen={(r[0],r[1]) for r in db.execute('SELECT case_id,version FROM digest_seen')}
            # A timed-out message may already be visible in the group. Suppress
            # blind re-delivery across dates, exposing it as uncertain in status.
            for row in db.execute("SELECT versions FROM digest_messages WHERE state IN ('uncertain','sending')"):
                seen.update(tuple(pair) for pair in json.loads(row[0]))
        return [c for c in cases if (c['id'],c['version']) not in seen]

    def tick(self, now, cases, pending=0):
        date=local_day(now);day=date.date().isoformat()
        self.initialize(now,cases)
        with self.store.db(True) as db:
            config=db.execute('SELECT * FROM daily_config').fetchone()
            db.execute("UPDATE digest_messages SET state='uncertain',last_error='上次发送结果未知，请核实群消息后处理' WHERE state='sending' AND lease<?",(now,))
        if day < config['first_send_day'] or date.hour < self.settings.daily_send_hour:
            return
        if not self.settings.wecom_digest_enabled:
            return
        if self.settings.wecom_transport == 'bot':
            from .wecom_state import bot_status
            bot = bot_status(self.settings, self.store, now)
            if (not getattr(self.transport, 'is_wecom_bot', False)
                    or not bot['bot_connected'] or not bot['group_bound']):
                return  # Only the dedicated long-connection worker can send.
            endpoint = 'https://wecom-bot.invalid/send'  # Intercepted locally; never sent over HTTP.
        elif self.settings.wecom_transport == 'webhook' and valid_webhook(self.settings.wecom_webhook_url):
            endpoint = self.settings.wecom_webhook_url
        else:
            return
        fresh=self.unseen(cases)
        pages=build_messages(day,fresh,self.settings.origin,pending)
        with self.store.db(True) as db:
            created=db.execute('INSERT OR IGNORE INTO digest_days VALUES(?,?)',(day,now)).rowcount
            if created:
                for number,page in enumerate(pages):
                    db.execute('INSERT INTO digest_messages(id,day,ordinal,content,versions) VALUES(?,?,?,?,?)',
                               (digest(['wecom-digest-v1',day,number]),day,number,page['content'],packed(page['versions'])))
            # Only one worker may send this day. At most one page per minute,
            # even with concurrent scheduler ticks or a restarted scheduler.
            if db.execute("SELECT 1 FROM digest_messages WHERE state='sending' OR (sent_at IS NOT NULL AND sent_at>?)",(now-60,)).fetchone():
                return
            row=db.execute("SELECT * FROM digest_messages WHERE day=? AND state!='sent' ORDER BY ordinal LIMIT 1",(day,)).fetchone()
            if not row or row['state']=='uncertain' or row['attempts']>=3 or row['retry_at']>now:
                return
            message=dict(row)
            db.execute("UPDATE digest_messages SET state='sending',attempts=attempts+1,lease=?,last_error='' WHERE id=?",(now+120,message['id']))
        state,error='uncertain','发送结果未知，请核实群消息；不会自动重发'
        try:
            with httpx.Client(timeout=httpx.Timeout(20,connect=8),trust_env=False,transport=self.transport,follow_redirects=False) as client:
                response=client.post(endpoint,json={'msgtype':'markdown','markdown':{'content':message['content']}})
            if response.status_code==200:
                data=response.json()
                if isinstance(data,dict) and data.get('errcode')==0:
                    state,error='sent',''
                elif isinstance(data,dict) and isinstance(data.get('errcode'),int):
                    state,error='failed','企业微信拒绝消息，错误码 '+str(data['errcode'])
            elif 400<=response.status_code<500:
                state,error='failed','企业微信 HTTP '+str(response.status_code)
        except (httpx.HTTPError,ValueError,TypeError):
            pass  # Never log request URLs, response bodies or webhook secrets.
        with self.store.db(True) as db:
            db.execute('UPDATE digest_messages SET state=?,last_error=?,lease=NULL,retry_at=?,sent_at=? WHERE id=?',
                       (state,error,now+300,now if state=='sent' else None,message['id']))
            if state=='sent':
                for case_id,version in json.loads(message['versions']):
                    db.execute('INSERT OR IGNORE INTO digest_seen VALUES(?,?,?,?)',(case_id,version,'sent',now))

    def status(self, now=None):
        now=time.time() if now is None else now
        with self.store.db() as db:
            config=db.execute('SELECT * FROM daily_config').fetchone()
            batch=db.execute('SELECT * FROM automation_daily_batches ORDER BY day DESC LIMIT 1').fetchone()
            last_day=db.execute('SELECT day FROM digest_days ORDER BY day DESC LIMIT 1').fetchone()
            rows=list(db.execute('SELECT * FROM digest_messages WHERE day=? ORDER BY ordinal',(last_day[0],))) if last_day else []
            uncertain=db.execute("SELECT count(*) FROM digest_messages WHERE state='uncertain'").fetchone()[0]
            if batch:
                batch=dict(batch)
                batch['asset_count']=db.execute('SELECT count(*) FROM automation_daily_assets WHERE day=?',(batch['day'],)).fetchone()[0]
        configured=valid_webhook(self.settings.wecom_webhook_url)
        from .wecom_state import bot_status
        bot = bot_status(self.settings, self.store, now) if self.settings.wecom_transport == 'bot' else {}
        from .case_library import list_cases
        pending_cases=len(self.unseen(list_cases(self.store)))
        state='ready'
        if self.settings.wecom_transport == 'bot':
            if not bot['bot_configured']:state='missing_bot'
            elif not self.settings.wecom_digest_enabled:state='disabled'
            elif not bot['bot_connected']:state='bot_disconnected'
            elif not bot['group_bound']:state='awaiting_group'
        elif not configured:state='missing_webhook'
        elif not self.settings.wecom_digest_enabled:state='disabled'
        if state=='ready' and rows:
            states={r['state'] for r in rows}
            state=next((s for s in ['uncertain','failed','sending','ready'] if s in states),'sent')
        return dict(enabled=self.settings.daily_batch_mode,timezone='Asia/Shanghai',
                    processing_time=f'{self.settings.daily_process_hour:02d}:00',send_time=f'{self.settings.daily_send_hour:02d}:00',
                    first_processing_day=config['first_processing_day'] if config else None,
                    first_send_day=config['first_send_day'] if config else None,
                    webhook_configured=configured,delivery_enabled=self.settings.wecom_digest_enabled,
                    transport=self.settings.wecom_transport,**bot,
                    last_batch=dict(batch) if batch else None,uncertain_messages=uncertain,pending_cases=pending_cases,
                    last_delivery=dict(day=last_day[0] if last_day else None,state=state,total_messages=len(rows),
                                       sent_messages=sum(r['state']=='sent' for r in rows),
                                       error=next((r['last_error'] for r in rows if r['last_error']),'')))
