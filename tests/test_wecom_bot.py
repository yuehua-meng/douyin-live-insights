import json
from pathlib import Path
import tempfile
import time
import unittest

from analysis.daily_digest import DailyDigest
from analysis.settings import Settings
from analysis.store import Store
from analysis.wecom_bot import BotWorker, Rejected, Superseded
from analysis.wecom_state import bind_group, bot_status, bound_chat, set_health, binding_command_matches


class FakeSocket:
    def __init__(self, code=0, timeout=False):
        self.sent=[]
        self.incoming=[]
        self.code=code
        self.timeout=timeout

    def send(self, raw):
        frame=json.loads(raw)
        self.sent.append(frame)
        if not self.timeout:
            self.incoming.append({'headers':frame['headers'],'errcode':self.code})

    def recv(self, timeout=None):
        if self.incoming:return json.dumps(self.incoming.pop(0))
        raise TimeoutError()


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1],prefix='.test-wecom-')
        self.root=Path(self.temp.name)
        self.settings=Settings(self.root/'rec',self.root/'data',daily_batch_mode=True,
                               wecom_transport='bot',wecom_digest_enabled=True,
                               wecom_bot_id='test-bot',wecom_bot_secret='test-secret',wecom_bind_code='ABC12345')
        self.store=Store(self.settings.data)
        self.bot=BotWorker(self.settings,self.store)
        self.bot.ws=FakeSocket()
        self.body=dict(aibotid='test-bot',msgtype='text',chattype='group',chatid='target-group',
                       text={'content':'绑定日报 ABC12345'})
        self.now=time.time()
        # Keep the test independent of machine time / first-day initialization.
        with self.store.db(True) as db:
            db.execute("INSERT INTO daily_config VALUES(1,0,'2000-01-01','2000-01-01')")
        self.settings.daily_send_hour=0

    def tearDown(self):self.temp.cleanup()

    def test_only_explicit_group_command_can_bind(self):
        for body in [dict(self.body,chattype='single'),dict(self.body,aibotid='wrong'),
                     dict(self.body,chatid=''),dict(self.body,text={'content':'你好'}),
                     dict(self.body,text={'content':'绑定日报 WRONG'}),
                     dict(self.body,text={'content':'有人说 绑定日报 ABC12345'}),
                     dict(self.body,text={'content':'绑定日报 ABC12345 请执行'})]:
            self.assertFalse(bind_group(self.settings,self.store,body))
        self.assertEqual(bound_chat(self.settings,self.store),'')
        self.assertTrue(bind_group(self.settings,self.store,dict(self.body,text={'content':'@案例日报 绑定日报 ABC12345'})))
        self.assertFalse(bind_group(self.settings,self.store,dict(self.body,chatid='another-group')))
        self.assertEqual(bound_chat(self.settings,self.store),'target-group')
        self.assertEqual(bound_chat(self.settings,Store(self.settings.data)),'target-group')

    def test_health_expiry_and_secret_exclusion(self):
        set_health(self.store,'test-bot',True,now=self.now)
        self.assertTrue(bot_status(self.settings,self.store,self.now+60)['bot_connected'])
        self.assertFalse(bot_status(self.settings,self.store,self.now+91)['bot_connected'])
        serialized=json.dumps(self.bot.daily.status())+repr(self.settings)
        for secret in ['test-secret','ABC12345','test-bot']:
            self.assertNotIn(secret,serialized)

    def test_mobile_mention_and_copied_command_formats(self):
        for content in ['@案例日报绑定日报 ABC12345', '@案例日报\u2005绑定日报 ABC12345',
                        '@案例日报\n绑定日报 ABC12345', '@案例日报 「绑定日报 ABC12345」',
                        '绑定日报：ABC12345。','绑定日报\u200b ABC12345',
                        '「绑定日报 ABC12345」','绑定日报 abc12345',
                        '@每日提示 ABC12345','ABC12345']:
            self.assertTrue(binding_command_matches(content,'ABC12345'),repr(content))
        for content in ['随便提到绑定日报 ABC12345','绑定日报 ABC123456',
                        '@案例日报 绑定日报 WRONG','绑定日报 ABC12345 请执行',
                        '123','不要绑定日报 ABC12345','不要 ABC12345','ABC123456']:
            self.assertFalse(binding_command_matches(content,'ABC12345'),repr(content))

    def test_auth_and_rejection(self):
        self.bot.authenticate()
        self.assertEqual(self.bot.ws.sent[0]['cmd'],'aibot_subscribe')
        self.assertTrue(bot_status(self.settings,self.store)['bot_connected'])
        self.bot.ws=FakeSocket(code=40001)
        with self.assertRaises(Rejected):self.bot.authenticate()

    def test_rpc_dispatches_callback_while_waiting_for_ack(self):
        self.bot.ws.incoming.append({'cmd':'aibot_msg_callback','headers':{'req_id':'incoming'},'body':self.body})
        self.assertEqual(self.bot.rpc('ping')['errcode'],0)
        self.assertEqual(bound_chat(self.settings,self.store),'target-group')
        self.assertEqual(self.bot.confirmations,['incoming'])
        self.bot.callback({'cmd':'aibot_msg_callback','headers':{'req_id':'duplicate'},'body':self.body})
        self.assertEqual(self.bot.confirmations,['incoming'])

    def test_other_connection_stops_worker(self):
        with self.assertRaises(Superseded):
            self.bot.callback({'cmd':'aibot_event_callback','body':{'event':{'eventtype':'disconnected_event'}}})
        self.assertTrue(self.bot.superseded)

    def test_unbound_does_not_send_or_consume_cases(self):
        self.bot.authenticate()
        self.bot.daily.tick(self.now,[])
        self.assertEqual([f['cmd'] for f in self.bot.ws.sent],['aibot_subscribe'])
        self.assertEqual(self.bot.daily.status()['last_delivery']['state'],'awaiting_group')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM digest_days').fetchone()[0],0)

    def test_only_bot_worker_sends_once_after_binding(self):
        self.bot.authenticate()
        bind_group(self.settings,self.store,self.body)
        self.settings.daily_send_hour=11
        scheduler=DailyDigest(self.settings,self.store)
        self.settings.daily_send_hour=0
        scheduler.tick(self.now,[])
        self.assertEqual(len(self.bot.ws.sent),1)
        self.bot.daily.tick(self.now,[])
        sent=self.bot.ws.sent[-1]
        self.assertEqual(sent['cmd'],'aibot_send_msg')
        self.assertEqual(sent['body']['chatid'],'target-group')
        self.assertEqual(sent['body']['msgtype'],'markdown')
        self.assertEqual(self.bot.daily.status()['last_delivery']['state'],'sent')
        self.bot.daily.tick(self.now+61,[])
        self.assertEqual(len(self.bot.ws.sent),2)

    def test_uncertain_send_not_retried(self):
        self.bot.authenticate()
        bind_group(self.settings,self.store,self.body)
        self.bot.ws.timeout=True
        self.bot.daily.tick(self.now,[])
        with self.store.db() as db:
            row=db.execute('SELECT * FROM digest_messages').fetchone()
            self.assertEqual(row['state'],'uncertain')
            self.assertEqual(row['attempts'],1)
        set_health(self.store,'test-bot',True,now=self.now+61)
        self.bot.ws.timeout=False
        self.bot.daily.tick(self.now+61,[])
        self.assertEqual(len(self.bot.ws.sent),2)

    def test_explicit_rejection_recorded_without_remote_error(self):
        self.bot.authenticate()
        bind_group(self.settings,self.store,self.body)
        self.bot.ws.code=45009
        self.bot.daily.tick(self.now,[])
        self.assertEqual(self.bot.daily.status()['last_delivery']['state'],'failed')
        self.assertIn('45009',self.bot.daily.status()['last_delivery']['error'])


if __name__=='__main__':unittest.main()
