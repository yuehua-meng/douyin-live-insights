import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from analysis.business import VERSION, evidence_from, packets, run, validate_extract, _index, _merge_batches, _merge_payload, _correct_ref_typos
from analysis.narration import NARRATION_VERSION, NARRATION_STYLE
from analysis.settings import Settings
from analysis.store import Store, packed, digest, BudgetExceeded


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='.test-business-',dir=Path(__file__).resolve().parents[1])
        self.root=Path(self.temp.name)
        self.settings=Settings(self.root/'recordings',self.root/'analysis',daily_budget=10000000,task_budget=1000000)
        self.store=Store(self.settings.data)
        self.job=self.store.create_job('summary',{'transcript_ids':['t1']})
        self.calls=[]
        self.t={'id':'t1','revision':1,'recording':{'id':'r1','path':'r1.m4a'},'duration_ms':60000,'status':'ready','segments':[
            {'id':'t1:0','start_ms':0,'end_ms':20000,'text':'客户咨询重复，公司希望员工少做重复整理。'},
            {'id':'t1:1','start_ms':20000,'end_ms':40000,'text':'我们讨论先整理资料，再给员工确认答复，暂未正式交付。'},
            {'id':'t1:2','start_ms':40000,'end_ms':60000,'text':'付费方式只是设想，还没有成交或者收益统计。'}]}

    def tearDown(self):self.temp.cleanup()

    def persist(self,result):
        with self.store.db(True) as db:
            db.execute('UPDATE jobs SET result=? WHERE id=?',(packed(result),self.job['id']))

    def project(self,refs,ident=''):
        return dict(id=ident,title='客户咨询整理',identity='同一客户咨询整理方案',stage='方案讨论',attribution='讨论视角',readiness='可讲述',
            facts=[dict(kind=kind,text=text,nature='计划',subject='讨论中的公司',refs=[refs[min(i,len(refs)-1)]]) for i,(kind,text) in enumerate([
                ('need','减少咨询重复整理'),('workflow','先整理资料，再由员工确认答复'),('commercial','付费是设想，尚未成交')])],missing_details=['正式交付结果'],conflicts=[])

    def response(self,request):
        body=json.loads(request.content);payload=json.loads(body['messages'][1]['content']);system=body['messages'][0]['content'];self.calls.append((system,payload))
        if '任务是提取业务事实' in system:
            refs=[e['id'] for e in payload['new_evidence']]
            output={'projects':[self.project(refs,payload['existing_projects'][0]['id'] if payload['existing_projects'] else '')],'observations':[]}
        elif '为老板整理商业总结' in system:
            facts=payload.get('facts',[])
            refs=facts[0]['refs'] if facts else payload['business_summaries'][0]['items'][0]['refs']
            output={'items':[{'heading':'需求与商业阶段','text':'计划整理客户咨询，由员工确认，目前尚未成交。','refs':refs}]}
        elif '本轮只选择项目身份' in system:
            output={'project_ids':[payload['projects'][0]['id']] if '咨询' in payload['requested_project'] else [],'reason':'未找到指定项目' if '咨询' not in payload['requested_project'] else ''}
        elif '核对项目目录' in system:output={'groups':[]}
        else:
            refs=[e['id'] for e in payload['raw_evidence']]
            output={'cases':[{'title':'客户咨询整理','stage':'方案讨论','text':'我们讨论的方案是先把客户咨询整理好，再由员工确认。付费方式仍是设想，尚未成交。','refs':refs,'missing_details':['正式交付结果']}],'reason':''}
        return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})

    def execute(self,previous=None,handler=None):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-only'}):
            return run(self.settings,self.store,self.job['id'],[self.t],lambda:None,lambda *_:None,previous=previous,on_result=self.persist,transport=httpx.MockTransport(handler or self.response))

    def test_business_facts_are_shared_and_writer_reads_original(self):
        output=self.execute()
        self.assertEqual(output['script']['prompt_version'],VERSION)
        self.assertEqual(len(output['script']['cases']),1)
        self.assertEqual(output['script']['cases'][0]['stage'],'方案讨论')
        self.assertIn('尚未成交',output['script']['cases'][0]['text'])
        self.assertEqual(len(self.calls),5)
        self.assertEqual(self.calls[-1][1]['raw_evidence'][2]['text'],self.t['segments'][2]['text'])
        self.assertEqual(self.calls[-1][1]['project']['attribution'],'讨论视角')
        self.assertEqual(output['summary']['transcript_ids'],['t1'])
        ids={e['id'] for e in output['summary']['evidence']}
        self.assertTrue(all(r in ids for r in output['script']['cases'][0]['refs']))
        self.assertTrue(all(e.startswith('S') for e in ids))
        again=self.execute()
        self.assertEqual(again,output)
        self.assertEqual(len(self.calls),5,'successful outputs and extraction must resume without new calls')
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET mode='business' WHERE id=?",(self.job['id'],))
        projects=self.store.project_catalog()
        self.assertEqual(len(projects),1)
        self.assertEqual(projects[0]['title'],'客户咨询整理')
        self.assertTrue(any(x['kind']=='need' for x in projects[0]['highlights']))

    def test_repairs_only_unique_one_character_evidence_typos(self):
        original='Saaaf0fc4aa38e82f701bb04b'
        typo='Saaaf0fc4aa38e82f701bb04'
        value={'items':[{'text':'客户需求','refs':[typo]}]}
        self.assertEqual(_correct_ref_typos(value,{original})['items'][0]['refs'],[original])
        self.assertEqual(_correct_ref_typos(value,{original,'Saaaf0fc4aa38e82f701bb04c'}),value)
        self.assertEqual(_correct_ref_typos(value,{'Sunrelated'}),value)

    def test_narration_upgrade_rewrites_old_style_only_and_resumes_without_cost(self):
        # An upgrade must invalidate both the stored case and paid write/review
        # caches, while keeping already extracted facts and the summary.
        with patch('analysis.business.NARRATION_VERSION','legacy-test-style'):
            old=self.execute()
        self.calls=[]
        upgraded=self.execute()
        self.assertEqual(upgraded['script']['narration_version'],NARRATION_VERSION)
        self.assertEqual(upgraded['summary'],old['summary'])
        self.assertEqual(upgraded['dossier']['projects'],old['dossier']['projects'])
        self.assertEqual(len(self.calls),3,'only write, packet review and final review should run again')
        for system,payload in self.calls:
            self.assertIn(NARRATION_STYLE,system)
            self.assertTrue(payload['raw_evidence'],'writer and reviews still receive original evidence')
        self.assertIn('本轮为复核',self.calls[1][0])
        self.assertIn('现在是全文最终检查',self.calls[2][0])
        self.assertNotEqual(upgraded['script']['cases'][0]['fact_version'],old['script']['cases'][0]['fact_version'])
        self.assertEqual(upgraded['script']['cases'][0]['narration_version'],NARRATION_VERSION)
        with self.store.db() as db:
            usage_before=db.execute('SELECT COUNT(*),SUM(actual) FROM usage').fetchone()
            usage_before=tuple(usage_before)
        self.calls=[]
        self.assertEqual(self.execute(),upgraded)
        self.assertEqual(self.calls,[])
        with self.store.db() as db:
            self.assertEqual(tuple(db.execute('SELECT COUNT(*),SUM(actual) FROM usage').fetchone()),usage_before)

    def test_pre_style_version_case_output_does_not_hide_upgrade(self):
        project=self.seed_projects(1)[0]
        result=self.store.job(self.job['id'])['result']
        legacy_project_hash=digest([VERSION,project['facts'],project['stage'],project['attribution'],
                                    project['conflicts'],project.get('source_conflicts'),project.get('narration_mode')])
        legacy_fingerprint=digest([legacy_project_hash,''])
        old_case=dict(title='旧版档案标题',stage='方案讨论',text='这是旧版口播正文。',
                      refs=[e['id'] for e in evidence_from([self.t])],missing_details=[],case_id=project['id'],
                      fact_version=legacy_fingerprint)
        result['dossier']['case_outputs'][project['id']]=dict(fingerprint=legacy_fingerprint,case=old_case,reason='')
        self.persist(result)
        output=self.execute()
        self.assertEqual(len(self.calls),3)
        self.assertNotEqual(output['script']['cases'][0]['text'],old_case['text'])
        self.assertEqual(output['script']['cases'][0]['narration_version'],NARRATION_VERSION)

    def test_narration_upgrade_preserves_locked_or_adopted_previous_case(self):
        with patch('analysis.business.NARRATION_VERSION','legacy-test-style'):
            old=self.execute()
        for flag in ['locked','adopted']:
            with self.subTest(flag=flag):
                previous=json.loads(json.dumps(old))
                previous['script']['cases'][0][flag]=True
                self.job=self.store.create_job('business',{'transcript_ids':['t1']})
                self.calls=[]
                output=self.execute(previous=previous)
                self.assertEqual(output['script']['cases'],previous['script']['cases'])
                self.assertEqual(self.calls,[],'approved previous drafts must not be rewritten during a style upgrade')

    def test_model_citation_typo_does_not_block_business_job(self):
        correct=evidence_from([self.t])[0]['id']
        typo=correct[:-1]
        def with_typo(request):
            response=self.response(request)
            sent=json.loads(request.content)
            system=sent['messages'][0]['content']
            if '为老板整理商业总结' not in system:
                return response
            data=response.json()
            content=json.loads(data['choices'][0]['message']['content'])
            content['items'][0]['refs']=[typo]
            data['choices'][0]['message']['content']=json.dumps(content)
            return httpx.Response(200,json=data)
        result=self.execute(handler=with_typo)
        self.assertEqual(result['summary']['items'][0]['refs'],[correct])
        self.assertEqual(len(result['script']['cases']),1)
        self.assertFalse(any('本轮只修正结构' in system for system,_ in self.calls))

    def test_long_case_response_retries_with_bounded_prose(self):
        def truncated_once(request):
            sent=json.loads(request.content)
            system=sent['messages'][0]['content']
            if '为老板准备可以直接出镜' in system and '单条正文最多2500个汉字' not in system:
                self.calls.append((system,json.loads(sent['messages'][1]['content'])))
                return httpx.Response(200,json={'usage':{'prompt_tokens':20,'completion_tokens':20},
                    'choices':[{'finish_reason':'length','message':{'content':'{"cases":['}}]})
            return self.response(request)
        output=self.execute(handler=truncated_once)
        self.assertEqual(len(output['script']['cases']),1)
        self.assertTrue(any('单条正文最多2500个汉字' in system for system,_ in self.calls))

    def test_recovery_after_later_failure_does_not_rebill_truncated_writer(self):
        from analysis.business import _call
        writes=[]
        def response(request):
            sent=json.loads(request.content);system=sent['messages'][0]['content']
            if '为老板准备可以直接出镜' in system and '本轮为复核' not in system:
                writes.append(system)
                if '单条正文最多2500个汉字' not in system:
                    return httpx.Response(200,json={'usage':{'prompt_tokens':20,'completion_tokens':20},
                        'choices':[{'finish_reason':'length','message':{'content':'{"cases":['}}]})
            return self.response(request)
        def later_failure(*args,**kwargs):
            if '本轮为复核' in kwargs.get('system',''):
                raise RuntimeError('simulated interruption after writer')
            return _call(*args,**kwargs)
        with patch('analysis.business._call',side_effect=later_failure):
            with self.assertRaisesRegex(RuntimeError,'simulated interruption'):
                self.execute(handler=response)
        self.assertEqual(len(writes),2)
        output=self.execute(handler=response)
        self.assertEqual(len(output['script']['cases']),1)
        self.assertEqual(len(writes),2,'resuming must not repay the truncated request or its successful fallback')

    def test_preexisting_concise_cache_is_revalidated_and_promoted(self):
        from analysis.business import _call, WRITE
        from analysis.script import validate_script
        from analysis.summarize import Summarizer
        from analysis.store import digest
        payload={'project':'cached project','raw_evidence':[{'id':'E1','text':'客户咨询整理方案'}]}
        version='cache-recovery-test'
        original=digest([version,self.settings.deepseek_model,payload,8000,'thinking-high'])
        fallback=digest([version+'-concise-retry-v1',self.settings.deepseek_model,payload,8000])
        cached={'cases':[{'title':'咨询整理','text':'我们讨论先整理，再由员工确认。','stage':'方案讨论','refs':['E1']}],'reason':''}
        self.store.cache('summaries',fallback,cached)
        runner=Summarizer(self.settings,self.store,self.job['id'],lambda:None,
                          httpx.MockTransport(lambda _:self.fail('must reuse existing successful fallback')))
        options=dict(system=WRITE,validator=validate_script,version=version,output_limit=8000,thinking=True)
        with self.assertRaises(ValueError):_call(runner,payload,{'different'},**options)
        self.assertIsNone(self.store.cached('summaries',original))
        output=_call(runner,payload,{'E1'},**options)
        self.assertEqual(output,validate_script(cached,{'E1'}))
        self.assertEqual(self.store.cached('summaries',original),output)
        self.assertEqual(_call(runner,payload,{'E1'},**options),output)
        self.assertEqual(self.store.spending()['committed'],0)

    def test_cross_chunk_project_uses_one_stable_case(self):
        with patch('analysis.business.READ_BYTES',100):
            # packets default is bound at definition time, so set the packet
            # wrapper's budget to force independent reading packets.
            original=packets
            with patch('analysis.business.packets',side_effect=lambda items,budget=100:original(items,budget)):
                output=self.execute()
        self.assertEqual(len(output['dossier']['projects']),1)
        self.assertEqual(len(output['dossier']['processed']),3)
        self.assertEqual(len(output['script']['cases']),1)
        extracts=[p for s,p in self.calls if '任务是提取业务事实' in s]
        self.assertEqual(len(extracts),3)
        self.assertTrue(extracts[1]['existing_projects'])
        self.assertTrue(extracts[1]['context'])
        self.assertEqual(extracts[1]['existing_projects'][0]['id'],output['script']['cases'][0]['case_id'])

    def test_technical_only_has_summary_but_no_invented_case(self):
        def response(request):
            body=json.loads(request.content);payload=json.loads(body['messages'][1]['content'])
            if '任务是提取业务事实' in body['messages'][0]['content']:
                refs=[payload['new_evidence'][0]['id']]
                value={'projects':[],'observations':[{'heading':'应用尚待明确','text':'仅讨论技术，未交代客户需求与业务流程。','refs':refs}]}
            else:value={'items':[{'heading':'业务材料不足','text':'暂无可确认的完整商业项目。','refs':payload['facts'][0]['refs']}]}
            self.calls.append(payload)
            return httpx.Response(200,json={'usage':{'prompt_tokens':20,'completion_tokens':20},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]})
        output=self.execute(handler=response)
        self.assertEqual(output['script']['cases'],[])
        self.assertIn('暂无',output['script']['reason'])
        self.assertEqual(len(self.calls),3)

    def test_failed_script_does_not_repeat_successful_summary(self):
        def failed(request):
            body=json.loads(request.content)
            if '为老板准备可以直接出镜' in body['messages'][0]['content']:
                return httpx.Response(503)
            return self.response(request)
        with self.assertRaisesRegex(RuntimeError,'503'):self.execute(handler=failed)
        self.assertIn('summary',self.store.job(self.job['id'])['result'])
        summaries=sum('为老板整理商业总结' in s for s,_ in self.calls)
        output=self.execute()
        self.assertEqual(sum('为老板整理商业总结' in s for s,_ in self.calls),summaries)
        self.assertEqual(len(output['script']['cases']),1)

    def test_revision_changes_stable_identity_and_rejects_bad_refs(self):
        old=evidence_from([self.t]);revised=dict(self.t,id='t1-revised')
        new=evidence_from([revised])
        self.assertTrue({e['id'] for e in old}.isdisjoint(e['id'] for e in new))
        self.assertEqual(evidence_from([self.t]),old)
        with self.assertRaises(ValueError):validate_extract({'projects':[self.project(['fake'])],'observations':[]},{old[0]['id']})
        invalid=self.project([old[0]['id']]);invalid['stage']='已赚一百万'
        with self.assertRaises(ValueError):validate_extract({'projects':[invalid],'observations':[]},{old[0]['id']})

    def test_empty_transcript_never_calls_paid_model(self):
        self.t['segments']=[]
        output=self.execute()
        self.assertEqual(self.calls,[])
        self.assertEqual(output['script']['cases'],[])

    def test_business_plan_can_be_told_without_claiming_owned_delivery(self):
        original=self.project
        def unclear(refs,ident=''):
            p=original(refs,ident);p.update(attribution='待核对',readiness='继续积累',conflicts=['发言人真实姓名未知'])
            return p
        def response(request):
            body=json.loads(request.content);payload=json.loads(body['messages'][1]['content'])
            if '判断当前项目能否以' in body['messages'][0]['content']:
                value={'ready':True,'refs':[e['id'] for e in payload['raw_evidence']],'reason':'需求和业务方案有依据，可用讨论视角','missing_details':['实际交付结果']}
                return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]})
            return self.response(request)
        with patch.object(self,'project',side_effect=unclear):output=self.execute(handler=response)
        self.assertEqual(len(output['script']['cases']),1)
        self.assertEqual(output['script']['cases'][0]['stage'],'方案讨论')
        self.assertEqual(output['dossier']['projects'][0]['attribution'],'待核对','原始事实归属不能被改写')
        writers=[p for s,p in self.calls if '为老板准备可以直接出镜' in s]
        self.assertEqual(writers[0]['project']['narration_mode'],'业务方案讨论')
        self.assertIn('发言人真实姓名未知',writers[0]['project']['source_conflicts'])

    def test_bad_model_reference_gets_one_budgeted_grounded_repair(self):
        def response(request):
            data=json.loads(request.content);payload=json.loads(data['messages'][1]['content'])
            if 'invalid_result' in payload:
                self.assertIn('fake-reference',json.dumps(payload['invalid_result']))
                original=payload['original_material']
                refs=[e['id'] for e in original['new_evidence']]
                output={'projects':[self.project(refs)],'observations':[]}
                return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})
            if '任务是提取业务事实' in data['messages'][0]['content']:
                output={'projects':[self.project(['fake-reference'])],'observations':[]}
                return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})
            return self.response(request)
        result=self.execute(handler=response)
        self.assertEqual(len(result['script']['cases']),1)
        real={e['id'] for e in result['script']['evidence']}
        self.assertTrue(all(r in real for c in result['script']['cases'] for r in c['refs']))

    def test_extraction_normalizes_benign_field_and_enum_variants(self):
        p=self.project(['s1'])
        p['facts']=[
            {'kind':' 客户需求 ','nature':'Fact','text':'咨询整理需求','refs':['s1']},
            {'type':'business_process','certainty':'proposed','content':'先整理后确认','refs':['s1']},
            {'kind':'商业模式','nature':'假设','text':'未来可能收费','refs':['s1']},
            {'category':'new_category','nature':'new_certainty','statement':'原文中的未知类型信息','refs':['s1']},
            {'kind':'Value','nature':None,'description':'没有明确性质的价值说法','refs':['s1']},
        ]
        facts=validate_extract({'projects':[p],'observations':[]},{'s1'})['projects'][0]['facts']
        self.assertEqual([f['kind'] for f in facts],['need','workflow','commercial','other','value'])
        self.assertEqual([f['nature'] for f in facts],['事实','计划','判断','未确认','未确认'])

    def test_extraction_diagnostics_are_specific_and_refs_stay_strict(self):
        p=self.project(['s1'])
        p['facts'][0]['text']={'private':'never echo model contents'}
        with self.assertRaisesRegex(ValueError,'text 类型应为文字（项目 1，事实 1）') as caught:
            validate_extract({'projects':[p],'observations':[]},{'s1'})
        self.assertNotIn('private',str(caught.exception))
        for refs in ['s1',['fake'],[],None]:
            bad=self.project(['s1']);bad['facts'][0]['refs']=refs
            with self.assertRaisesRegex(ValueError,'有效原文依据'):
                validate_extract({'projects':[bad],'observations':[]},{'s1'})

    def test_requested_project_is_selected_and_missing_topic_never_falls_back(self):
        with self.store.db(True) as db:
            db.execute('UPDATE jobs SET payload=? WHERE id=?',(packed({'script_topic':'咨询项目'}),self.job['id']))
        output=self.execute()
        self.assertEqual(len(output['script']['cases']),1)
        self.assertEqual(output['script']['requested_project'],'咨询项目')
        self.assertTrue(any('本轮只选择项目身份' in s for s,_ in self.calls))
        # A new immutable task with another topic may share extraction cache,
        # but must not silently show the unrelated consultation case.
        self.job=self.store.create_job('summary',{'script_topic':'报销审批'})
        self.calls=[]
        missing=self.execute(previous=output)
        self.assertEqual(missing['script']['cases'],[])
        self.assertIn('未找到',missing['script']['reason'])
        self.assertFalse(any('为老板准备可以直接出镜' in s for s,_ in self.calls))

    def test_large_case_reviews_every_raw_packet_then_whole_story(self):
        with patch('analysis.business.WRITE_BYTES',400):output=self.execute()
        ids={e['id'] for e in evidence_from([self.t])}
        reviewed={e['id'] for s,p in self.calls if '本轮为复核' in s for e in p['raw_evidence']}
        self.assertEqual(reviewed,ids)
        final=[p for s,p in self.calls if '现在是全文最终检查' in s]
        self.assertEqual(len(final),1)
        self.assertEqual(set(final[0]['previously_checked_source_ids']),ids)
        self.assertEqual(len(output['script']['cases']),1)

    def seed_projects(self,count=4,unclear=False,all_cases=True):
        refs=[e['id'] for e in evidence_from([self.t])]
        projects=[]
        for i in range(count):
            p=self.project(refs,'P'+str(i));p.update(title='咨询整理项目'+str(i),identity='独立项目'+str(i))
            if unclear:p.update(readiness='继续积累',attribution='待核对')
            projects.append(p)
        with self.store.db(True) as db:
            db.execute('UPDATE jobs SET payload=? WHERE id=?',(packed({'all_cases':all_cases}),self.job['id']))
        self.persist({'dossier':dict(version=VERSION,projects=projects,processed=refs,audited=refs,merged=True,case_outputs={}),
                      'summary':dict(items=[],evidence=evidence_from([self.t]),prompt_version=VERSION)})
        return projects

    def test_all_cases_completes_more_than_three_without_repeated_calls(self):
        self.seed_projects(5)
        output=self.execute()
        self.assertEqual(len(output['script']['cases']),5)
        self.assertEqual({c['case_id'] for c in output['script']['cases']},{'P'+str(i) for i in range(5)})
        self.assertEqual(output['script']['processing'],dict(complete=True,total=5,processed=5,remaining=0))
        before=len(self.calls)
        self.assertEqual(self.execute(),output)
        self.assertEqual(len(self.calls),before)

    def test_all_cases_checks_beyond_first_three_and_remembers_rejections(self):
        self.seed_projects(5,unclear=True)
        checked=[]
        def response(request):
            data=json.loads(request.content);payload=json.loads(data['messages'][1]['content'])
            if '判断当前项目能否以' in data['messages'][0]['content']:
                ident=payload['project']['id'];checked.append(ident)
                value={'ready':ident=='P4','refs':[e['id'] for e in payload['raw_evidence']],
                       'reason':'业务主线尚不明确' if ident!='P4' else '操作链有直接依据','missing_details':[]}
                return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]})
            return self.response(request)
        output=self.execute(handler=response)
        self.assertEqual(checked,['P'+str(i) for i in range(5)])
        self.assertEqual([c['case_id'] for c in output['script']['cases']],['P4'])
        self.assertEqual(sum(e['state']=='insufficient' for e in output['script']['evaluations']),4)
        self.assertEqual(len(output['dossier']['case_assessments']),5)
        self.execute(handler=response)
        self.assertEqual(len(checked),5,'unchanged rejected projects must not be repeatedly reassessed')

    def test_completed_cases_remain_visible_when_later_case_hits_budget(self):
        self.seed_projects(4)
        from analysis.business import _call
        def limited(runner,payload,allowed,**kwargs):
            if payload.get('project',{}).get('id')=='P1':
                raise BudgetExceeded('本批预算不足，保留已完成案例')
            return _call(runner,payload,allowed,**kwargs)
        with patch('analysis.business._call',side_effect=limited):
            with self.assertRaises(BudgetExceeded):self.execute()
        partial=self.store.job(self.job['id'])['result']
        self.assertEqual([c['case_id'] for c in partial['script']['cases']],['P0'])
        self.assertFalse(partial['script']['processing']['complete'])
        self.assertEqual(partial['script']['processing']['remaining'],3)
        first_writes=sum(p.get('project',{}).get('id')=='P0' for _,p in self.calls)
        output=self.execute()
        self.assertEqual(len(output['script']['cases']),4)
        self.assertEqual(sum(p.get('project',{}).get('id')=='P0' for _,p in self.calls),first_writes)

    def test_review_rejection_is_persisted_and_not_rewritten(self):
        self.seed_projects(1)
        def response(request):
            self.calls.append(('rejected',{}))
            value={'cases':[],'reason':'需求与实施方案无法确认属于同一项目'}
            return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]})
        output=self.execute(handler=response)
        self.assertEqual(output['script']['cases'],[])
        self.assertIn('同一项目',output['script']['evaluations'][0]['reason'])
        before=len(self.calls)
        self.execute(handler=response)
        self.assertEqual(len(self.calls),before)

    def test_history_passes_forty_projects_with_bounded_complete_directory(self):
        projects=self.seed_projects(40,all_cases=True)
        for p in projects:
            p['facts']=p['facts'][:1]
            p.update(readiness='继续积累',identity='项目身份信息'*100,missing_details=['信息缺口'*200])
        saved=self.store.job(self.job['id'])['result']
        saved['dossier'].update(projects=projects,processed=[],audited=[])
        saved['dossier'].pop('merged',None)
        self.persist(saved)
        merge_seen=[]
        def response(request):
            body=json.loads(request.content);payload=json.loads(body['messages'][1]['content']);system=body['messages'][0]['content']
            if 'audit' in payload:
                value={'projects':[],'observations':[dict(heading='已有讨论',text='没有新增项目',refs=[e['id'] for e in payload['new_evidence']])]}
            elif '任务是提取业务事实' in system:
                self.assertEqual(len(payload['existing_projects']),40)
                project=self.project([e['id'] for e in payload['new_evidence']]);project['identity']='新的独立业务项目'
                project['facts']=project['facts'][:1];project['readiness']='继续积累'
                value={'projects':[project],'observations':[]}
            elif '核对项目目录' in system:
                self.assertLessEqual(len(packed(payload).encode()),60000)
                merge_seen.extend(p['id'] for p in payload['projects'])
                value={'groups':[]}
            else:return self.response(request)
            return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]})
        output=self.execute(handler=response)
        self.assertEqual(len(output['dossier']['projects']),41)
        self.assertEqual(output['script']['processing']['processed'],41)
        # A final singleton needs no merge call, but is still evaluated/written.
        self.assertEqual(len(set(merge_seen)),40)
        directory=_index([dict(projects[0],id='PX'+str(i)) for i in range(200)])
        self.assertEqual(len(directory),200)
        self.assertLess(len(packed(directory).encode()),125000)
        self.assertEqual({p['id'] for p in directory},{'PX'+str(i) for i in range(200)})

    def test_merge_groups_include_every_project_with_bounded_originals(self):
        projects=[];evidence=[]
        for i in range(65):
            refs=[]
            for j in range(2):
                ref='S'+str(i)+'-'+str(j);refs.append(ref)
                evidence.append(dict(id=ref,text='原文业务依据'*300,recording_id='r'+str(i)))
            projects.append(self.project(refs,'P'+str(i)))
        batches=list(_merge_batches(projects,evidence))
        self.assertEqual([p['id'] for group in batches for p in group],[p['id'] for p in projects])
        self.assertTrue(all(len(packed(_merge_payload(group,evidence)).encode())<=60000 for group in batches))

    def test_new_unrelated_recording_reuses_previous_case_and_rejection(self):
        self.seed_projects(2)
        initial=self.store.job(self.job['id'])['result']
        initial['dossier']['projects'][1].update(readiness='继续积累',attribution='待核对')
        self.persist(initial)
        checks=[]
        def response(request):
            body=json.loads(request.content);payload=json.loads(body['messages'][1]['content']);system=body['messages'][0]['content']
            if '判断当前项目能否以' in system:
                checks.append(payload['project']['id'])
                value={'ready':False,'refs':[],'reason':'业务流程归属未明确','missing_details':[]}
            elif '任务是提取业务事实' in system:
                value={'projects':[],'observations':[dict(heading='闲聊',text='无新增项目',refs=[payload['new_evidence'][0]['id']])]}
            else:return self.response(request)
            return httpx.Response(200,json={'usage':{'prompt_tokens':30,'completion_tokens':30},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]})
        first=self.execute(handler=response)
        original_writes=sum('为老板准备可以直接出镜' in s for s,_ in self.calls)
        self.job=self.store.create_job('business',{'all_cases':True,'transcript_ids':['t1','t2']})
        second=dict(self.t,id='t2',recording={'id':'r2','path':'r2.m4a'},segments=[dict(id='t2:0',start_ms=0,end_ms=1000,text='欢迎下次再聊。')])
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-only'}):
            result=run(self.settings,self.store,self.job['id'],[self.t,second],lambda:None,lambda *_:None,
                       previous=first,on_result=self.persist,transport=httpx.MockTransport(response))
        self.assertEqual(checks,['P1'])
        self.assertEqual(sum('为老板准备可以直接出镜' in s for s,_ in self.calls),original_writes)
        self.assertEqual(result['script']['cases'],first['script']['cases'])


if __name__=='__main__':unittest.main()
