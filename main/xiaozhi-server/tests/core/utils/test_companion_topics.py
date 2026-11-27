"""Q4 expression separation, sourced topic lifecycle and actual A/B inputs."""
import hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from types import SimpleNamespace as NS
from core.providers.memory.powermem.store import Store
from core.utils.companion_store import TopicStore,EpisodeStore
from core.utils.companion_topics import (validate_update,prepare_topic_turn,topic_context,topic_context_current,router_topic_extension,explicit_preferences)
from core.utils.companion_expression import select_dialogue_policy,render_expression_messages,PERSONA,EXAMPLES,PUBLIC_PROTOCOL
import test_natural_response as natural
import test_companion_response as q1


def update(text='',action='none',topic='',target='',preferences=None,pending=''):
    return dict(topic_action=action,topic_id=target,topic_label=topic,pending_point=pending,evidence_quote=text,preferences=preferences or [])
def pref(key,value,quote):return dict(key=key,value=value,quote=quote)


class TopicStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ledger=Store(Path(self.temp.name)/'test.sqlite');self.store=TopicStore(self.ledger)
        with self.ledger.db() as db:self.epoch=self.ledger._epoch(db,'person')
    def tearDown(self):self.temp.cleanup()
    def apply(self,turn,value,session='call'):
        return self.store.apply_topic('person','xiaoyuan',self.epoch,session,turn,value,3600)
    def rows(self):return self.store.catalog('person','xiaoyuan')[1]

    def test_state_is_independent_of_fact_affect_and_episode_tables(self):
        self.apply('one',update('我担心朋友',action='new',topic='朋友'))
        self.assertEqual(self.ledger.current('person'),[])
        with self.ledger.db() as db:
            names={r[0] for r in db.execute('SELECT name FROM sqlite_master')}
        self.assertNotIn('companion_affect',names);self.assertNotIn('companion_episodes',names)

    def test_quoted_third_party_and_hypothetical_are_not_current_controls(self):
        for text,quote in [('他说“别问了”，你觉得什么意思？','别问了'),('如果我说别问了你会怎样？','如果我说别问了'),('他让我别问了。','他让我别问了。')]:
            v=validate_update(update(preferences=[pref('questions_allowed',False,quote)]),text,set())
            self.assertEqual(v['preferences'],[])
        self.assertIsNone(validate_update(update(preferences=[pref('questions_allowed',False,'伪造原话')]),'别问了。',set()))

    def test_distinct_preferences_have_real_quotes_and_explicit_resume(self):
        text='别给我出主意，也别问了，你先听我说。'
        v=validate_update(update(text,action='new',topic='倾诉',preferences=[
            pref('questions_allowed',False,'别问了'),pref('advice_allowed',False,'别给我出主意'),pref('listening_only',True,'你先听我说')]),text,set())
        self.apply('one',v);row=self.rows()[0];state=json.loads(row['user_preference'])
        self.assertFalse(state['questions_allowed']['value']);self.assertTrue(state['listening_only']['value'])
        text='现在帮我想个办法。';v=validate_update(update(text,action='continue',target=row['id'],preferences=[
            pref('advice_allowed',True,text),pref('listening_only',False,text)]),text,{row['id']})
        self.apply('two',v);state=json.loads(self.rows()[0]['user_preference'])
        self.assertTrue(state['advice_allowed']['value']);self.assertFalse(state['questions_allowed']['value'])
        self.assertFalse(state['listening_only']['value'])

    def test_explicit_reinvitation_is_recovered_without_extra_model_call(self):
        text='现在帮我想一个不太生分的开场白吧。'
        values={r['key']:r for r in explicit_preferences(text)}
        self.assertTrue(values['advice_allowed']['value']);self.assertFalse(values['listening_only']['value'])
        self.assertTrue(all(r['quote'] in text for r in values.values()))

    def test_local_commands_respect_last_clause_and_reported_quotes(self):
        values={r['key']:r for r in explicit_preferences('别再给我建议，现在帮我想个办法。')}
        self.assertTrue(values['advice_allowed']['value'])
        for text in ['他说“别问了”，你觉得什么意思？','如果我说别问了你会怎样？','他让我别问了。','你“别问”问我是什么含义？']:
            self.assertEqual(explicit_preferences(text),[],text)

    def test_new_topic_does_not_inherit_previous_topic_preferences(self):
        self.apply('one',update('别问了',action='new',topic='烦恼',preferences=[pref('questions_allowed',False,'别问了')]))
        _,before=self.store.topic_context('person','xiaoyuan',self.epoch,set(),3)
        self.apply('two',update('咱们换个话题说说画',action='new',topic='画'))
        selected,after=self.store.topic_context('person','xiaoyuan',self.epoch,set(),3)
        self.assertFalse(self.store.topic_context_current('person','xiaoyuan',before))
        self.assertEqual(len(selected),1);self.assertEqual(json.loads(selected[0]['user_preference']),{})
        self.assertTrue(self.store.topic_context_current('person','xiaoyuan',after))

    def test_decline_then_user_reopens_same_topic(self):
        self.apply('one',update('先不聊朋友了',action='new',topic='朋友'))
        row=self.rows()[0];self.apply('two',update('先不聊了',action='decline',target=row['id']))
        self.assertEqual(self.rows()[0]['status'],'declined')
        self.apply('three',update('朋友那件事我又想说说',action='continue',target=row['id']),session='another-device')
        self.assertEqual(self.rows()[0]['status'],'pending')

    def test_unknown_ids_invalid_quotes_and_resolution_without_evidence(self):
        self.assertIsNone(validate_update(update('我想说说',action='continue',target='other-person'),'我想说说',set()))
        self.assertIsNone(validate_update(update('并不存在',action='new',topic='无'),'我想说说',set()))
        self.assertEqual(validate_update(update('你给个建议',action='resolve'),'你给个建议',set())['topic_action'],'none')
        self.assertEqual(validate_update(update('已经解决了',action='resolve'),'已经解决了',set())['topic_action'],'resolve')

    def test_same_turn_is_idempotent_and_cross_role_scope_are_isolated(self):
        v=update('我担心朋友',action='new',topic='朋友');self.apply('one',v)
        revision=self.rows()[0]['revision'];self.apply('one',v)
        self.assertEqual(len(self.rows()),1);self.assertEqual(self.rows()[0]['revision'],revision)
        self.assertEqual(self.store.catalog('other','xiaoyuan')[1],[])
        self.assertEqual(self.store.catalog('person','other-role')[1],[])

    def test_forget_removes_state_and_old_ticket_cannot_recreate_it(self):
        self.apply('one',update('我担心朋友',action='new',topic='朋友'))
        _,token=self.store.topic_context('person','xiaoyuan',self.epoch,set(),3)
        self.ledger.forget('person',[])
        self.assertEqual(self.rows(),[]);self.assertFalse(self.store.topic_context_current('person','xiaoyuan',token))
        self.assertFalse(self.apply('two',update('旧来源',action='new',topic='旧话题')))

    def test_expiry_and_empty_snapshot_detect_new_current_topic(self):
        _,token=self.store.topic_context('person','xiaoyuan',self.epoch,set(),3)
        self.apply('one',update('朋友的事',action='new',topic='朋友'))
        self.assertFalse(self.store.topic_context_current('person','xiaoyuan',token))
        with self.store.db() as db:db.execute('UPDATE companion_topics SET expires_at=?',(time.time()-1,))
        self.assertEqual(self.rows(),[])

    def test_preferences_and_source_state_survive_process_wrapper_restart(self):
        self.apply('one',update('别问了',action='new',topic='朋友',preferences=[pref('questions_allowed',False,'别问了')]))
        reopened=TopicStore(Store(Path(self.temp.name)/'test.sqlite'))
        self.assertFalse(json.loads(reopened.catalog('person','xiaoyuan')[1][0]['user_preference'])['questions_allowed']['value'])

    def test_only_retrieved_unfinished_episode_is_synced_and_withdrawn_on_revision(self):
        from core.utils.companion_memory import EpisodePolicy,validate_episode
        import test_companion_memory as ep
        es=EpisodeStore(self.ledger);p=EpisodePolicy('xiaoyuan')
        ticket=es.reserve_episode('person','xiaoyuan',p.fingerprint,'old-call','t','我担心兰花养不好。')
        es.finish_episode(ticket,'咱们一起想想。');job=es.claim_episode('person','xiaoyuan',p.fingerprint)
        es.apply_episode(job,ep.result(),[],90)
        from core.utils.companion_memory import read_episode_context
        ctx,_=read_episode_context(es,'person','xiaoyuan','兰花')
        self.store.sync_episode_topics('person','xiaoyuan',self.epoch,ctx['records'],3600)
        rows,token=self.store.topic_context('person','xiaoyuan',self.epoch,{ctx['records'][0]['id']},3)
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['source_episode'])
        row=es.episode_records('person','xiaoyuan')[1][0]
        es.maintain_episode('person','xiaoyuan',row['id'],row['revision'],delete=True)
        self.assertFalse(self.store.topic_context_current('person','xiaoyuan',token))
        self.assertEqual(self.store.topic_context('person','xiaoyuan',self.epoch,{row['id']},3)[0],[])


class TopicWorkflowTests(unittest.IsolatedAsyncioTestCase):
    add=natural.NaturalTests.add;chat=natural.NaturalTests.chat;texts=staticmethod(natural.NaturalTests.texts)
    def setUp(self):
        natural.NaturalTests.setUp(self)
        self.role='测试用老人陪伴角色。';self.hashpatch=patch('core.utils.companion_expression.SOURCE_ROLE_SHA256',hashlib.sha256(self.role.encode()).hexdigest());self.hashpatch.start()
    def tearDown(self):self.hashpatch.stop();natural.NaturalTests.tearDown(self)
    def connection(self,direct=False):
        c=q1.RoleWorkflowTests.connection(self)
        c.config['dialogue_quality']['strategy']='role_direct' if direct else 'legacy'
        c.config['companion_dialogue']=dict(enabled=True,allowed_subject_scopes=[self.memory.scope])
        c.dialogue.update_system_message('<identity>\n'+self.role+'\n</identity>\n<conversation_protocol>旧交流层</conversation_protocol>\n<capability_protocol>保留实际能力边界</capability_protocol>')
        return c

    def test_scoped_render_keeps_template_role_example_responsibilities_separate(self):
        c=self.connection();original=[dict(role='system',content=c.dialogue.dialogue[0].content),dict(role='user',content='现在说的话')]
        result=render_expression_messages(c,original)
        self.assertIn(PERSONA,result[0]['content']);self.assertIn(PUBLIC_PROTOCOL,result[0]['content'])
        self.assertIn('保留实际能力边界',result[0]['content']);self.assertIn('不属于当前用户历史',result[0]['content'])
        self.assertEqual(result[-1],original[-1]);self.assertNotIn(PERSONA,original[0]['content'])
        self.assertEqual(len(result),2)

    def test_other_role_or_disabled_or_denied_scope_has_no_candidate_or_table(self):
        c=self.connection();c.config['companion_dialogue']['enabled']=False
        self.assertIsNone(select_dialogue_policy(c));self.assertIsNone(router_topic_extension(c))
        with self.engine.store.db() as db:self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='companion_topics'").fetchone())
        c.config['companion_dialogue']['enabled']=True;c.memory._memory_scope_denied=True
        self.assertIsNone(select_dialogue_policy(c));del c.memory._memory_scope_denied
        c.dialogue.update_system_message('<identity>另一个角色</identity>');self.assertIsNone(select_dialogue_policy(c))

    async def test_actual_a_and_b_receive_candidate_and_quoted_topic_constraints(self):
        for direct in (False,True):
            c=self.connection(direct);c.session_id='call-'+str(direct)
            text='别问了，你先听我说。'
            await self.chat(c,text,dict(route='chat',input_origin='asr',dialogue_update=update(text,action='new',topic='倾诉',preferences=[
                pref('questions_allowed',False,'别问了'),pref('listening_only',True,'你先听我说')])))
            if direct:
                messages=c.role_calls[-1];self.assertIn(PERSONA,messages[0]['content']);self.assertIn('companion_dialogue',str(messages));self.assertFalse(c.review_calls)
            else:
                data=c.review_calls[-1]['current_turn_data'];self.assertFalse(data['companion_dialogue']['records'][0]['user_preference']['questions_allowed']['value'])
                self.assertIn(PERSONA,c.role_calls[0][0]['content'])
            self.assertNotIn('好，我听着',self.texts(c)) # Model fixture text, never backend canned reply.

    async def test_explicit_new_topic_clears_only_its_previous_preferences(self):
        c=self.connection();text='别问了。'
        await self.chat(c,text,dict(route='chat',input_origin='asr',dialogue_update=update(text,action='new',topic='朋友',preferences=[pref('questions_allowed',False,text)])))
        text='换个话题说说画。'
        await self.chat(c,text,dict(route='chat',input_origin='asr',dialogue_update=update(text,action='new',topic='画')))
        state=c.review_calls[-1]['current_turn_data']['companion_dialogue']['records']
        self.assertEqual(len(state),1);self.assertEqual(state[0]['user_preference'],{});self.assertEqual(state[0]['topic'],'画')

    async def test_router_omitted_resume_is_restored_in_actual_final_input(self):
        c=self.connection();text='别给我出主意，你先听我说。'
        await self.chat(c,text,dict(route='chat',input_origin='asr',dialogue_update=update(text,action='new',topic='倾诉')))
        text='现在帮我想一个开场白吧。'
        await self.chat(c,text,dict(route='chat',input_origin='asr',dialogue_update=update(text,action='continue')))
        state=c.review_calls[-1]['current_turn_data']['companion_dialogue']['records'][0]
        self.assertTrue(state['user_preference']['advice_allowed']['value'])
        self.assertFalse(state['user_preference']['listening_only']['value'])
        self.assertTrue(any('重新邀请建议' in line for line in state['interaction_notes']))

    async def test_a_rephrases_internal_analysis_without_reading_it_aloud(self):
        c=self.connection();seen=[]
        def compose(system,payload,schema,**kw):
            data=json.loads(payload);seen.append(data)
            answer='我偏小曲。\n理由解析：\n1.节奏。' if len(seen)==1 else '我偏小曲，慢慢听着舒服。'
            return json.dumps(dict(valid=True,reply=answer,reason='',news_numbers=[]))
        c.intent.llm.response_structured=compose
        await self.chat(c,'你自己偏哪种？',dict(route='chat',input_origin='asr'))
        self.assertEqual(len(seen),2);self.assertIn('内部旁白',seen[-1]['format_correction'])
        self.assertEqual(self.texts(c),'我偏小曲，慢慢听着舒服。')

    async def test_bad_draft_is_withdrawn_not_reinserted_as_style_on_retry(self):
        c=self.connection();c.llm.response=lambda *a,**kw:iter(['我喜欢小曲。\n**理由解析：**很多原因。'])
        seen=[]
        def compose(system,payload,schema,**kw):
            data=json.loads(payload);seen.append(data)
            return json.dumps(dict(valid=True,reply='我偏小曲。\n理由解析：很多原因。' if len(seen)==1 else '我偏小曲，听着松快。',reason='',news_numbers=[]))
        c.intent.llm.response_structured=compose
        await self.chat(c,'你偏哪种音乐？',dict(route='chat',input_origin='asr'))
        self.assertTrue(all(r['draft']=='' for r in seen));self.assertEqual(len(seen),2)
        self.assertEqual(self.texts(c),'我偏小曲，听着松快。')

    async def test_direct_character_repairs_format_with_same_author_once(self):
        c=self.connection(direct=True);calls=[]
        def role(session,messages,**kw):
            calls.append(messages)
            return iter(['我偏小曲。\n理由解析：\n1.节奏。' if len(calls)==1 else '我偏小曲，听着松快。'])
        c.llm.response=role
        await self.chat(c,'你偏哪种？',dict(route='chat',input_origin='asr'))
        self.assertEqual(len(calls),2);self.assertFalse(c.review_calls)
        self.assertEqual(calls[-1][-1]['role'],'user');self.assertIn('内部旁白',calls[-1][-2]['content'])
        self.assertEqual(self.texts(c),'我偏小曲，听着松快。')

    async def test_grounded_weather_failure_feedback_requests_a_new_usable_sentence(self):
        c=self.connection();seen=[]
        def compose(system,payload,schema,**kw):
            data=json.loads(payload);seen.append(data)
            return json.dumps(dict(valid=True,reply='今儿个天气正好，出来走走吧。' if len(seen)==1 else '老伙计，最近忙什么呢？',reason='',news_numbers=[]))
        c.intent.llm.response_structured=compose
        await self.chat(c,'帮我想个开场白。',dict(route='chat',input_origin='asr'))
        self.assertIn('不依赖天气事实',seen[-1]['format_correction'])
        self.assertEqual(self.texts(c),'老伙计，最近忙什么呢？')

    async def test_router_extends_existing_call_once_and_returns_sourced_metadata(self):
        from core.utils.information_router import ReadOnlySession
        c=self.connection();calls=[];text='别问了，你先听我说。'
        def route(system,payload,schema,**kw):
            calls.append(json.loads(payload));self.assertIn('dialogue_update',schema['required'])
            return json.dumps(dict(route='chat',requests=[],clarification_slot=None,memory_query=None,user_fact_statement=False,related_context_needed=False,
                dialogue_update=update(text,action='new',topic='倾诉',preferences=[pref('questions_allowed',False,'别问了')])))
        c.intent.llm.response_structured=route
        plan=await ReadOnlySession(c).plan(text)
        self.assertEqual(len(calls),1);self.assertEqual(plan['_dialogue_update']['preferences'][0]['quote'],'别问了')
        self.assertIn('topic_catalog',calls[0])

    async def test_bad_optional_metadata_does_not_break_main_route(self):
        from core.utils.information_router import ReadOnlySession
        c=self.connection()
        c.intent.llm.response_structured=lambda *a,**kw:json.dumps(dict(route='chat',requests=[],clarification_slot=None,memory_query=None,
            user_fact_statement=False,related_context_needed=False,dialogue_update=update('虚构原话',action='new',topic='假')))
        plan=await ReadOnlySession(c).plan('今天想随便聊聊。')
        self.assertEqual(plan['route'],'chat');self.assertIsNone(plan['_dialogue_update'])

    async def test_forget_during_generation_withdraws_topic_on_retry(self):
        c=self.connection();text='别问了。';seen=[]
        def compose(data):
            seen.append(data)
            if len(seen)==1:self.engine.store.forget(self.memory.scope,[])
            return dict(valid=True,reply='按本轮交流。',reason='')
        original=c.intent.llm.response_structured
        def wrapped(system,payload,schema,**kw):
            d=json.loads(payload);v=compose(d);v['news_numbers']=[];return json.dumps(v)
        c.intent.llm.response_structured=wrapped
        await self.chat(c,text,dict(route='chat',input_origin='asr',dialogue_update=update(text,action='new',topic='倾诉',preferences=[pref('questions_allowed',False,text)])))
        self.assertEqual(len(seen),2);self.assertNotIn('companion_dialogue',seen[-1]['current_turn_data'])
        with self.engine.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM companion_topics').fetchone()[0],0)

    def test_rebinding_disabling_and_stale_generation_block_topic_use(self):
        c=self.connection();c.sentence_id='t';c.lookup_generation=1
        ticket=prepare_topic_turn(c,dict(generation=1,input_origin='asr'),'我想说说。','t')
        self.assertIsNotNone(ticket);self.memory.scope='another'
        self.assertEqual(topic_context(c,ticket,None),(None,None));self.memory.scope='synthetic'
        c.config['companion_dialogue']['enabled']=False;self.assertEqual(topic_context(c,ticket,None),(None,None))
        c.config['companion_dialogue']['enabled']=True
        self.assertIsNone(prepare_topic_turn(c,dict(generation=0,input_origin='asr'),'旧轮次','t'))

    async def test_manager_preserves_local_dialogue_rollout_block(self):
        from config import config_loader
        with patch.object(config_loader,'init_service'),patch.object(config_loader,'get_server_config',new=AsyncMock(return_value={'server':{'auth':{'enabled':True}}})):
            block=dict(enabled=True,allowed_subject_scopes=['synthetic'])
            result=await config_loader.get_config_from_api_async({'manager-api':{'url':'https://example.invalid'},'companion_dialogue':block})
        self.assertEqual(result['companion_dialogue'],block)
