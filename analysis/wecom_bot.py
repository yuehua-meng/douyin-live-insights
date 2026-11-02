"""WeCom smart-bot long connection, explicit group binding and daily outbox.

Protocol: https://github.com/WecomTeam/aibot-node-sdk
Never log credentials, message bodies, callbacks or WebSocket exception text.
"""
import json
import signal
import threading
import time
import uuid

import httpx
from websockets.sync.client import connect

from .case_library import list_cases
from .daily_digest import DailyDigest
from .settings import Settings
from .store import Store
from .wecom_state import bind_group, bound_chat, set_health

ENDPOINT = 'wss://openws.work.weixin.qq.com'


class Superseded(Exception):
    """Another client owns this bot. Stop rather than fight over its connection."""


class Rejected(Exception):
    def __init__(self, code):
        self.code = code if type(code) is int else -1


class BotTransport(httpx.BaseTransport):
    is_wecom_bot = True

    def __init__(self, bot):
        self.bot = bot

    def handle_request(self, request):
        chat = bound_chat(self.bot.settings, self.bot.store)
        if not chat:
            return httpx.Response(409, json={'errcode':-1}, request=request)
        payload = json.loads(request.content)
        try:
            ack = self.bot.rpc('aibot_send_msg', {'chatid':chat, **payload})
        except Exception:
            # A sent frame may have reached the group despite a lost receipt.
            self.bot.healthy = False
            set_health(self.bot.store, self.bot.settings.wecom_bot_id, False, '连接中断，正在重连')
            raise httpx.ReadTimeout('Smart-bot delivery outcome unknown', request=request) from None
        # Only expose the numeric code, not the remote error text.
        return httpx.Response(200, json={'errcode':ack.get('errcode')}, request=request)


class BotWorker:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.ws = None
        self.healthy = False
        self.superseded = False
        self.confirmations = []
        self.stop = threading.Event()
        self.daily = DailyDigest(settings, store, BotTransport(self))

    def callback(self, frame):
        body = frame.get('body')
        if not isinstance(body, dict):
            return
        if frame.get('cmd') == 'aibot_event_callback':
            event = body.get('event')
            if isinstance(event, dict) and event.get('eventtype') == 'disconnected_event':
                self.superseded = True
                raise Superseded()
        if frame.get('cmd') == 'aibot_msg_callback':
            text = body.get('text')
            content = text.get('content', '') if isinstance(text, dict) else ''
            # Diagnostic flags only: no conversation content, identifiers or credentials.
            print('WeCom callback metadata: '+json.dumps({
                'group':body.get('chattype') == 'group',
                'text':body.get('msgtype') == 'text',
                'bot_matches':body.get('aibotid') == self.settings.wecom_bot_id,
                'has_chat':bool(body.get('chatid')),
                'binding_code_present':bool(self.settings.wecom_bind_code and isinstance(content,str)
                                            and self.settings.wecom_bind_code in content),
                'starts_with_mention':isinstance(content,str) and content.strip().startswith('@'),
            }),flush=True)
        if frame.get('cmd') == 'aibot_msg_callback' and bind_group(self.settings, self.store, body):
            req_id = (frame.get('headers') or {}).get('req_id')
            if isinstance(req_id, str) and req_id:
                self.confirmations.append(req_id)
            print('Daily digest group bound.', flush=True)

    def read_frame(self, timeout):
        raw = self.ws.recv(timeout=timeout)
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return frame if isinstance(frame, dict) else {}

    def rpc(self, cmd, body=None, req_id=None):
        req_id = req_id or cmd+'_'+uuid.uuid4().hex
        frame = {'cmd':cmd, 'headers':{'req_id':req_id}}
        if body is not None:
            frame['body'] = body
        self.ws.send(json.dumps(frame,ensure_ascii=False))
        deadline = time.monotonic()+10
        while True:
            left = deadline-time.monotonic()
            if left <= 0:
                raise TimeoutError()
            answer = self.read_frame(left)
            if ((answer.get('headers') or {}).get('req_id') == req_id
                    and 'errcode' in answer and not answer.get('cmd', '').endswith('_callback')):
                return answer
            self.callback(answer)

    def authenticate(self):
        ack = self.rpc('aibot_subscribe', {'bot_id':self.settings.wecom_bot_id,
                                         'secret':self.settings.wecom_bot_secret})
        if ack.get('errcode') != 0:
            raise Rejected(ack.get('errcode'))
        self.healthy = True
        set_health(self.store, self.settings.wecom_bot_id, True)
        print('WeCom smart bot authenticated.', flush=True)

    def connected_loop(self):
        heartbeat, digest_due = time.monotonic()+30, 0
        while not self.stop.is_set() and self.healthy and not self.superseded:
            now = time.monotonic()
            if now >= heartbeat:
                ack = self.rpc('ping')
                if ack.get('errcode') != 0:
                    raise Rejected(ack.get('errcode'))
                set_health(self.store,self.settings.wecom_bot_id,True)
                heartbeat = time.monotonic()+30
            if self.confirmations:
                request_id = self.confirmations.pop(0)
                text = (f'日报群已绑定。每天北京时间 {self.settings.daily_process_hour:02d}:00 开始整理新录音，'
                        f'{self.settings.daily_send_hour:02d}:00 推送已完成的口播稿与商业总结。'
                        '未完成内容保留进度，纳入后续日报。')
                ack = self.rpc('aibot_respond_msg', {'msgtype':'stream', 'stream':{
                    'id':uuid.uuid4().hex, 'finish':True, 'content':text}}, req_id=request_id)
                code = ack.get('errcode')
                print('Group binding confirmation receipt: '+str(code if type(code) is int else 'unknown'),flush=True)
            if now >= digest_due:
                if bound_chat(self.settings,self.store):
                    self.daily.tick(time.time(),list_cases(self.store))
                digest_due = time.monotonic()+60
            if not self.healthy or self.superseded:
                break
            try:
                self.callback(self.read_frame(1))
            except TimeoutError:
                pass

    def run(self):
        delay = 2
        while not self.stop.is_set():
            try:
                with connect(ENDPOINT, proxy=None, open_timeout=15, close_timeout=3,
                             ping_interval=None, max_size=1_048_576) as ws:
                    self.ws = ws
                    self.authenticate()
                    delay = 2
                    self.connected_loop()
                if self.superseded:
                    raise Superseded()
            except Superseded:
                set_health(self.store,self.settings.wecom_bot_id,False,'机器人已被其他连接接管，请检查重复部署')
                print('Bot connection superseded; worker stopped.',flush=True)
                return
            except Rejected as exc:
                message = ('Bot ID 或 Secret 无效，请在企业微信保存机器人并核对凭据（853000）'
                           if exc.code == 853000 else '企业微信连接拒绝，错误码 '+str(exc.code))
                set_health(self.store,self.settings.wecom_bot_id,False,message)
                print('WeCom authentication/heartbeat rejected, code '+str(exc.code),flush=True)
                delay = 60
            except Exception as exc:
                set_health(self.store,self.settings.wecom_bot_id,False,'连接中断，正在重连')
                print('WeCom reconnect: '+type(exc).__name__,flush=True)
            finally:
                self.healthy = False
                self.ws = None
            if not self.stop.is_set():
                self.stop.wait(delay)
                delay = min(60,delay*2)
        set_health(self.store,self.settings.wecom_bot_id,False,'连接服务已停止')


def main():
    settings = Settings.load()
    if settings.wecom_transport != 'bot' or not settings.wecom_bot_id or not settings.wecom_bot_secret:
        raise SystemExit('Smart bot is not configured')
    worker = BotWorker(settings,Store(settings.data))
    for name in (signal.SIGINT,signal.SIGTERM):
        signal.signal(name,lambda *_: worker.stop.set())
    worker.run()


if __name__ == '__main__':
    main()
