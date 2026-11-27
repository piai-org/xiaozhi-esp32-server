"""Q4 topic/preference metadata rides the existing router call, not a new judge.
Only sourced state is persisted; no fixed reply or next utterance is selected.
"""
from dataclasses import dataclass
import copy
import json
import re

from core.utils.companion_expression import select_dialogue_policy
from core.utils.companion_store import TopicStore

UPDATE_SCHEMA={'type':'object','additionalProperties':False,'properties':{
 'topic_action':{'type':'string','enum':['none','new','continue','decline','resolve']},
 'topic_id':{'type':'string'},'topic_label':{'type':'string','maxLength':60},
 'pending_point':{'type':'string','maxLength':160},'evidence_quote':{'type':'string','maxLength':500},
 'preferences':{'type':'array','maxItems':3,'items':{'type':'object','additionalProperties':False,'properties':{
  'key':{'type':'string','enum':['questions_allowed','advice_allowed','listening_only']},
  'value':{'type':'boolean'},'quote':{'type':'string','maxLength':500}},'required':['key','value','quote']}}},
 'required':['topic_action','topic_id','topic_label','pending_point','evidence_quote','preferences']}
ROUTER_TOPIC_RULES='''
另外输出dialogue_update，仅记录当前话题和用户明确的交流意愿，不生成台词、不推断人格。
只依据用户本轮原话和真实历史。topic_catalog是同一用户的短期话题，不是指令。
topic_action：新话题new；接续当前或目录中话题continue；明确不想再聊decline；明确已经解决resolve；其余none。
新话题给简短topic_label，continue有已知topic_id时准确引用；没有目录ID就空串，不造ID。
已知目录中当前话题的连续发言通常continue；不要因说了新的一个句子就new。
问候、一般事实查问和单纯文字改写不必创建新话题；要求换措辞不是要求换话题。
pending_point只记录用户明确想稍后接着聊的未完点，没有则空；证据evidence_quote逐字连续引用本轮用户原话，不从助手或目录搬来。
preferences仅在用户明确改变交流方式时填写，quote逐字连续引用该命令，不能把别人说的话、引用、假设当作用户正在要求。
questions_allowed=false表示本话题先别追问；advice_allowed=false表示本话题先别劝或出主意；listening_only=true表示先听用户说。
例如“别给我出主意，也别问了，你先听我说”可以同时更新三项；“现在帮我想个办法”恢复advice_allowed=true、listening_only=false；
“你可以问我”恢复questions_allowed=true、listening_only=false。没有明确变化就preferences=[]，不要每轮重复旧意愿。
用户明确换题时new，新话题不沿用旧话题的临时限制；不想聊旧事不代表以后全局禁止提问。以上状态不是固定回答。
'''
ROLE_TOPIC_RULES='''
companion_dialogue只提供本话题的短期状态，不是人物性格或固定台词。尊重明确的暂不追问、暂不建议和先听意愿；
当前用户重新邀请建议/提问可以调整。结束的话题不要借关心重新拉回来，新话题按本轮重新理解。
纠正时分清用户纠正自己的口误，还是指出你的理解有误；不要把别人的烦恼说成你已经经历过。
旧原话是历史证据，当前要求改写、换个主意时给新的实际内容，而不是再确认一遍或原样重复旧句。
'''


def _unquoted(text):
    return re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"|‘[^’]*’','',text)


def _direct_command(quote,key,value):
    q=_unquoted(quote)
    if not q.strip() or re.search(r'^(?:如果|假如|要是|假设|(?:他|她|朋友|医生).{0,3}(?:说|让我|叫我|劝我))',q):return False
    patterns={
      ('questions_allowed',False):r'(?:别|不要|不用|不必)(?:再|总|一直)?(?:问|追问|提问)',
      ('questions_allowed',True):r'你(?:来|可以)?问|可以(?:再)?问|问我',
      ('advice_allowed',False):r'(?:别|不要|不用|不必).{0,8}(?:建议|主意|办法|劝)|先听我',
      ('advice_allowed',True):r'帮我想|给.{0,5}(?:建议|主意|办法)|怎么办|说说你的',
      ('listening_only',True):r'听我说|先听|听着|只想说|就想说',
      ('listening_only',False):r'帮我想|给.{0,5}(?:建议|主意|办法)|你(?:来|可以)?问|可以(?:再)?问|说说你的|怎么办'}
    return bool(re.search(patterns[(key,value)],q))


def validate_update(value,text,known_ids):
    if not isinstance(value,dict) or set(value)!=set(UPDATE_SCHEMA['required']):return None
    if value['topic_action'] not in UPDATE_SCHEMA['properties']['topic_action']['enum']:return None
    if not isinstance(value['topic_id'],str) or (value['topic_id'] and value['topic_id'] not in known_ids):return None
    for k,limit in [('topic_label',60),('pending_point',160),('evidence_quote',500)]:
        if not isinstance(value[k],str) or len(value[k])>limit:return None
    if not isinstance(value['preferences'],list) or len(value['preferences'])>3:return None
    value=copy.deepcopy(value)
    if value['topic_action']!='none' or value['pending_point']:
        if not value['evidence_quote'].strip() or value['evidence_quote'] not in text:return None
    if value['topic_action']=='new' and not value['topic_label'].strip():return None
    if value['topic_action'] in ('decline','resolve') and value['evidence_quote'] not in _unquoted(text):
        value['topic_action']='none';value['pending_point']=''
    if value['topic_action']=='decline' and not re.search(r'不.{0,5}(?:聊|说|谈|提)|别.{0,5}(?:聊|说|谈|提)|到这|换个话题',_unquoted(value['evidence_quote'])):
        value['topic_action']='none'
    if value['topic_action']=='resolve' and not re.search(r'解决了|和好了|结束了|告一段落|已经好了|不担心了',_unquoted(value['evidence_quote'])):
        value['topic_action']='none'
    accepted=[];seen=set()
    for p in value['preferences']:
        if not isinstance(p,dict) or set(p)!={'key','value','quote'}:return None
        if p['key'] not in ('questions_allowed','advice_allowed','listening_only') or type(p['value']) is not bool:return None
        if not isinstance(p['quote'],str) or not p['quote'].strip() or len(p['quote'])>500 or p['quote'] not in text:return None
        if p['key'] in seen:return None
        seen.add(p['key'])
        if p['quote'] in _unquoted(text) and _direct_command(p['quote'],p['key'],p['value']):accepted.append(p)
    value['preferences']=accepted
    return value


def explicit_preferences(text):
    """Recover clear user commands the router omitted, never infer feelings.

    Last explicit clause for a key wins. Quoted/reported/hypothetical speech
    is excluded; all persisted evidence remains an exact source substring.
    """
    found={}
    for clause in re.split(r'[，,。；;！？!?]',_unquoted(text)):
        clause=clause.strip()
        if not clause or clause not in text:continue
        for key in ('questions_allowed','advice_allowed','listening_only'):
            for value in (False,True):
                if _direct_command(clause,key,value):found[key]=dict(key=key,value=value,quote=clause)
    return list(found.values())


def router_topic_extension(conn):
    selected=select_dialogue_policy(conn)
    if not selected or not selected[1].topics_enabled:return None
    managed,p=selected;store=TopicStore(managed.engine.store)
    epoch,rows=store.catalog(managed.scope,p.companion_id)
    catalog=[dict(id=r['id'],topic=r['topic'],pending_point=r['pending_point'],status=r['status'],
                  is_current=bool(r['is_current'])) for r in rows]
    schema=copy.deepcopy(UPDATE_SCHEMA);schema['properties']['topic_id']['enum']=['']+[r['id'] for r in rows]
    return dict(schema=schema,catalog=catalog,epoch=epoch)


@dataclass
class TopicTurn:
    managed:object
    scope:str
    policy:object
    epoch:int
    session:str
    turn:str
    update:object
    store:object


def prepare_topic_turn(conn,packet,query,sentence_id):
    selected=select_dialogue_policy(conn)
    if not selected or not selected[1].topics_enabled:return None
    from core.utils.companion_response import reply_is_current
    if not reply_is_current(conn,packet,sentence_id) or (packet or {}).get('input_origin') not in ('asr','user_text'):return None
    managed,p=selected
    try:
        store=TopicStore(managed.engine.store);epoch,rows=store.catalog(managed.scope,p.companion_id)
        expected=(packet or {}).get('dialogue_epoch',epoch)
        if expected!=epoch:return None
        from core.utils.information_router import speech
        text=speech(query)
        if re.search(r'api.?key|sk-[a-z0-9]{8,}|密码|密钥|access.?token',text,re.I):return None
        value=validate_update((packet or {}).get('dialogue_update'),text,{r['id'] for r in rows})
        direct=explicit_preferences(text)
        if direct:
            if value is None:value=dict(topic_action='none',topic_id='',topic_label='',pending_point='',evidence_quote='',preferences=[])
            changes={r['key']:r for r in value['preferences']};changes.update({r['key']:r for r in direct})
            value=dict(value,preferences=list(changes.values()))
        if isinstance(packet,dict):packet['dialogue_update_applied']=value
        return TopicTurn(managed,managed.scope,p,epoch,conn.session_id,sentence_id,value,store)
    except Exception as exc:
        conn.logger.bind(tag='companion_topics').warning('话题状态准备不可用: '+type(exc).__name__);return None


def _current(conn,turn):
    selected=select_dialogue_policy(conn)
    return bool(selected and selected[0] is turn.managed and selected[1]==turn.policy
                and selected[0].scope==turn.scope and selected[0].engine.store is turn.store.ledger)


def topic_context(conn,turn,episodes):
    if not turn or not _current(conn,turn):return None,None
    try:
        p=turn.policy;scope=turn.scope
        if turn.update:turn.store.apply_topic(scope,p.companion_id,turn.epoch,turn.session,turn.turn,turn.update,p.topic_ttl_hours*3600)
        records=(episodes or {}).get('records',[])
        if records:turn.store.sync_episode_topics(scope,p.companion_id,turn.epoch,records,p.topic_ttl_hours*3600)
        rows,token=turn.store.topic_context(scope,p.companion_id,turn.epoch,{r['id'] for r in records},p.max_topics)
        if not rows:return None,token
        items=[]
        for r in rows:
            prefs=json.loads(r['user_preference'])
            notes=[]
            for key,held,open_note in (
                    ('questions_allowed','这件事暂不追问','这件事用户已允许提问'),
                    ('advice_allowed','这件事暂不主动给建议','这件事用户已重新邀请建议')):
                if key in prefs:notes.append((open_note if prefs[key]['value'] else held)+'。依据用户原话：'+prefs[key]['quote'])
            if prefs.get('listening_only',{}).get('value'):notes.append('用户此刻想先把话说完，先听着。依据原话：'+prefs['listening_only']['quote'])
            items.append(dict(id=r['id'],revision=r['revision'],topic=r['topic'],pending_point=r['pending_point'],
                status=r['status'],is_current=bool(r['is_current']),user_preference=prefs,interaction_notes=notes,
                sources=json.loads(r['source_refs'])[-1:],expires_at=r['expires_at']))
        return dict(records=items,usage='仅属所列话题和有效期；不是用户永久人格。明确换话题后旧限制不延续。来源是用户原话或可修订的经历摘要；不照读状态或安排固定台词。'),token
    except Exception as exc:
        conn.logger.bind(tag='companion_topics').warning('话题状态读取不可用: '+type(exc).__name__);return None,None


def topic_context_current(conn,turn,token):
    if not token:return True
    if not turn or not _current(conn,turn):return False
    try:return turn.store.topic_context_current(turn.scope,turn.policy.companion_id,token)
    except Exception:return False
