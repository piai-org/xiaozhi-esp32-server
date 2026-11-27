"""Q5: sourced delivery metadata, no model call and no speech-text rewrite.
A task is opened after its first final speech text is known, because the actual
provider accepts instruction on run-task, not as a spoken text prefix.
"""
from collections import OrderedDict
from dataclasses import dataclass,asdict
import hashlib,json,math,re,time
from core.providers.tts.dto.dto import SpeechDelivery,SentenceType,ContentType

QWEN_INSTRUCTION_MODELS=frozenset(('qwen-audio-3.1-tts-flash','qwen-audio-3.1-tts-plus','qwen-audio-3.0-tts-flash','qwen-audio-3.0-tts-plus'))
DIRECTIONS={
 'plain':None,
 'warm':'自然亲切地交流，语气温和，不用播报腔，停顿自然，完整读清原文字词。',
 'bright':'带着轻松真诚的高兴说话，明快但不夸张，不喊叫，完整读清原文字词。',
 'calm':'语气平和、体贴、沉稳，轻轻接住对方的话，不煽情、不拖长尾音，完整读清原文字词。',
 'serious':'语气认真、清楚、平稳，不渲染紧张，完整读清原文字词。',
}

@dataclass(frozen=True)
class SpeechPolicy:
    companion_id:str='xiaoyuan'
    mode:str='natural'
    slow_rate:float=.85
    fast_rate:float=1.15
    ttl_seconds:float=60
    planning:str='rules'
    @property
    def fingerprint(self):
        values=asdict(self);planning=values.pop('planning')
        if planning!='rules':values['planning']=planning
        return hashlib.sha256(json.dumps(dict(version='q5-v1',**values),sort_keys=True).encode()).hexdigest()


def select_speech_policy(conn):
    cfg=getattr(conn,'config',{}).get('companion_speech')
    if not isinstance(cfg,dict) or cfg.get('enabled') is not True:return None
    memory=getattr(conn,'memory',None);managed=getattr(memory,'_managed',memory)
    if not getattr(memory,'managed',False) or getattr(memory,'_memory_scope_denied',False):return None
    if not getattr(managed,'managed',False) or not isinstance(getattr(managed,'scope',None),str) or not managed.scope:return None
    if getattr(conn,'intent_type',None)!='intent_llm' or not getattr(getattr(conn,'intent',None),'read_only',False):return None
    try:
        from core.utils.information_response import model_reply_enabled
        if not model_reply_enabled(conn):return None
        devices,scopes=cfg.get('allowed_device_ids',[]),cfg.get('allowed_subject_scopes',[])
        if any(not isinstance(v,list) or any(not isinstance(s,str) or not s for s in v) for v in (devices,scopes)):return None
        if conn.device_id not in devices and managed.scope not in scopes:return None
        companion=cfg.get('companion_id','xiaoyuan');mode=cfg.get('mode','natural');planning=cfg.get('planning','rules')
        if not isinstance(companion,str) or not re.fullmatch(r'[\w.-]{1,80}',companion) or mode not in ('natural','plain'):return None
        if planning not in ('rules','model'):return None
        caps=conn.tts.speech_capabilities()
        if not caps.get('instruction') or caps.get('provider')!='alibl_stream':return None
        values={}
        for key,default,low,high in [('slow_rate',.85,.5,1),('fast_rate',1.15,1,2),('ttl_seconds',60,5,180)]:
            value=cfg.get(key,default)
            if type(value) not in (float,int) or not math.isfinite(value) or not low<=value<=high:return None
            values[key]=value
        return managed,SpeechPolicy(companion,mode,planning=planning,**values)
    except (AttributeError,TypeError,ValueError):return None


def _rate_request(query,p):
    # Explicit current request only, not an inferred permanent preference.
    text=re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"','',str(query or ''))
    chosen=None
    for clause in re.split(r'[，,。；;！？!?]',text):
        clause=clause.strip()
        if re.match(r'^(?:你|您)?(?:能不能|可以|请|麻烦)?(?:说|讲|读|念)(?:得|话)?(?:再|稍微|稍)?慢(?:一?点|些)|^慢点(?:说|讲|读|念)',clause):chosen=p.slow_rate
        if re.match(r'^(?:你|您)?(?:能不能|可以|请|麻烦)?(?:说|讲|读|念)(?:得|话)?(?:再|稍微|稍)?快(?:一?点|些)|^快点(?:说|讲|读|念)',clause):chosen=p.fast_rate
    return chosen


def _direction(conn,text,policy,turn):
    if policy.mode=='plain':return 'plain','plain_mode'
    # These describe explicit features of final role text, not ASR prosody or
    # the user's internal emotion. Current words outrank stale happy history.
    if re.search(r'急救|立即.{0,8}(?:联系|求助)|认真.{0,5}(?:说|讲)|安全',text):return 'serious','final_text'
    if re.search(r'难过|难受|委屈|担心|堵得慌|憋屈|心里堵|我听着|我在这儿听着|我在这里听着|慢慢说|安静|歇会|不是滋味|高兴不起来|开心不起来|笑不出来',text):return 'calm','final_text'
    if re.search(r'恭喜|太好了|真高兴|哈哈|唱痛快|嘴皮子.*利索',text):return 'bright','final_text'
    from core.utils.companion_affect import affect_context,decayed
    bundle=getattr(conn,'_affect_turns',{}).get(turn)
    if bundle and bundle[0].policy.companion_id==policy.companion_id:
        state=affect_context(conn,bundle)
        # Only a gentle slow-varying background; never turn old joy into a
        # forced cheerful voice in a new, potentially serious exchange.
        if state:
            row=bundle[0]._row()
            observed=decayed(row,bundle[0].policy) if row else None
            if observed and observed['intensity']>=bundle[0].policy.min_intensity and observed['emotion'] in ('concern','sadness','calm'):
                return 'calm','q2_context'
    return 'warm','neutral_context'


def model_planning_enabled(conn):
    selected=select_speech_policy(conn)
    return bool(selected and selected[1].mode=='natural' and selected[1].planning=='model')


def _issue_delivery(conn,text,query,turn_id,selected,style,instruction,source,plan=None):
    managed,p=selected
    from core.utils.information_router import speech
    delivery=SpeechDelivery(turn_id,getattr(conn,'lookup_generation',0),managed.scope,managed.delivery_token()[0],p.companion_id,p.fingerprint,
        style,instruction,_rate_request(speech(query),p),hashlib.sha256(text.encode()).hexdigest(),time.monotonic(),source,plan)
    cache=getattr(conn,'_speech_deliveries',None)
    if cache is None:cache=conn._speech_deliveries=OrderedDict()
    cache[turn_id]=delivery
    while len(cache)>16:cache.popitem(last=False)
    return delivery


def bind_model_delivery(conn,packet,text,query,value,turn_id,expected_epoch):
    """Bind only a final, evidence-validated result. No retries/model calls here."""
    if not isinstance(packet,dict):return None
    packet.pop('_speech_plan',None)
    try:
        if not model_planning_enabled(conn) or packet.get('runtime_event'):return None
        from core.utils.companion_response import reply_is_current
        from core.utils.speech_plan import validate_speech_plan,compile_speech_plan
        selected=select_speech_policy(conn)
        if (not reply_is_current(conn,packet,turn_id) or expected_epoch is None
                or selected[0].delivery_token()[0]!=expected_epoch):return None
        if value is None:packet['speech_plan_status']='fallback_missing';return None
        plan=validate_speech_plan(value,text)
        delivery=_issue_delivery(conn,text,query,turn_id,selected,'model',compile_speech_plan(plan),'final_author',plan)
        if delivery.epoch!=expected_epoch:return None
        packet['_speech_plan']=delivery;packet['speech_plan_status']='model'
        return delivery
    except Exception:
        packet['speech_plan_status']='fallback_invalid'
        return None


def make_delivery(conn,text,query,packet,turn_id):
    selected=select_speech_policy(conn)
    if not selected or (packet or {}).get('runtime_event'):return None
    from core.utils.companion_response import reply_is_current
    if not reply_is_current(conn,packet,turn_id):return None
    managed,p=selected
    candidate=(packet or {}).get('_speech_plan')
    if (p.planning=='model' and isinstance(candidate,SpeechDelivery)
            and candidate.style=='model' and candidate.text_sha256==hashlib.sha256(text.encode()).hexdigest()
            and compile_delivery(conn,conn.tts,candidate,turn_id)):
        return candidate
    style,source=_direction(conn,text,p,turn_id)
    return _issue_delivery(conn,text,query,turn_id,selected,style,DIRECTIONS[style],source)


def compile_delivery(conn,provider,delivery,turn_id):
    """Only current, application-issued plans become real provider parameters."""
    if not isinstance(delivery,SpeechDelivery):return {}
    selected=select_speech_policy(conn)
    if not selected:return {}
    managed,p=selected
    if (delivery.turn_id!=turn_id or getattr(conn,'sentence_id',None)!=turn_id or getattr(conn,'client_abort',False)
            or delivery.generation!=getattr(conn,'lookup_generation',None) or delivery.subject_scope!=managed.scope
            or delivery.policy!=p.fingerprint or delivery.companion_id!=p.companion_id
            or getattr(conn,'_speech_deliveries',{}).get(turn_id) is not delivery
            or not 0<=time.monotonic()-delivery.issued_at<=p.ttl_seconds):return {}
    if delivery.epoch!=managed.delivery_token()[0]:return {}
    caps=provider.speech_capabilities()
    if not caps.get('instruction') or caps.get('model') not in QWEN_INSTRUCTION_MODELS:return {}
    if delivery.style=='model':
        if p.planning!='model' or p.mode!='natural':return {}
        try:
            from core.utils.speech_plan import compile_speech_plan
            if delivery.instruction!=compile_speech_plan(delivery.plan):return {}
        except (ValueError,TypeError,KeyError,AttributeError):return {}
    elif delivery.style not in DIRECTIONS or delivery.instruction!=DIRECTIONS[delivery.style]:return {}
    params={}
    if delivery.instruction:params['instruction']=delivery.instruction
    if delivery.rate is not None:
        if type(delivery.rate) not in (float,int) or not math.isfinite(delivery.rate) or not .5<=delivery.rate<=2:return {}
        params['rate']=delivery.rate
    return params


class SpeechQueue:
    def __init__(self,conn,query,packet,turn):
        self.conn,self.query,self.packet,self.turn=conn,query,packet,turn
        self.selected=select_speech_policy(conn);self.first=None;self.delivery=None;self.started=False
        self.scope=self.selected[0].scope if self.selected else None
        self.epoch=self.selected[0].delivery_token()[0] if self.selected else None
        self.raw=conn.tts.tts_text_queue.put

    def put(self,message):
        if not self.selected:self.raw(message);return
        from core.utils.companion_response import reply_is_current
        managed,_=self.selected
        memory=getattr(self.conn,'memory',None)
        epoch=managed.delivery_token()[0]
        if not self.started and (self.packet or {}).get('_speech_epoch')==epoch:
            self.epoch=epoch
        if (message.sentence_id!=self.turn or not reply_is_current(self.conn,self.packet,self.turn)
                or managed.scope!=self.scope or getattr(memory,'_memory_scope_denied',False)
                or epoch!=self.epoch):return
        if message.sentence_type==SentenceType.FIRST:
            self.first=message;return
        if not self.started:
            if message.sentence_type==SentenceType.LAST and not message.content_detail and not message.content_file:
                self.first=None;return
            if self.first is None:return
            text=message.content_detail or ''
            try:self.delivery=make_delivery(self.conn,text,self.query,self.packet,self.turn)
            except Exception as exc:
                logger=getattr(self.conn,'logger',None)
                if logger:logger.bind(tag='companion_speech').warning('朗读方向不可用，使用原合成参数: '+type(exc).__name__)
            self.first.delivery=self.delivery;self.raw(self.first);self.first=None;self.started=True
        message.delivery=self.delivery;self.raw(message)
