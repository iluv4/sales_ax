"""Conversation -> versioned ICP proposal. Offline mode is explicitly rule-based, never fake AI."""
import json
import os
from typing import Literal
from pydantic import BaseModel,Field,ConfigDict
from .db import now
from .jobs import JobQueue


class PersonaCard(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=200)
    customer_type:Literal['A_EXPERT_PARTNER','B_END_CUSTOMER']='A_EXPERT_PARTNER'
    motion:Literal['EXPERT_PARTNER','CORPORATE_AX','EDU_ALLIANCE','CREATOR_PARTNER']='EXPERT_PARTNER'
    industries:list[str]=Field(default_factory=list)
    buyer_titles:list[str]=Field(default_factory=lambda:['대표','현업 전문가'])
    regions:list[str]=Field(default_factory=lambda:['대한민국'])
    channels:list[str]=Field(default_factory=lambda:['공식 홈페이지','공개 콘텐츠'])
    pain_hypotheses:list[str]=Field(default_factory=list)
    product:str='전문가 노하우 기반 AI 업무 시스템 및 공동 실습 교육'
    value_prop:str='현장 판단 기준을 재사용 가능한 AI 업무 시스템과 공동 교육으로 연결하는 협업 제안'
    disqualifiers:list[str]=Field(default_factory=lambda:['공개 업무 연락 근거 없음','거절·수신거부','이미 제안한 동일 연락처'])
    search_queries:list[str]=Field(default_factory=list)
    unknowns:list[str]=Field(default_factory=list)
    fact_policy:str='성과·학력·매출·인력·계약조건은 공개 근거 확인 전 사용하지 않음'


def draft_persona(text):
    # A small editable bootstrap, preserving the original user message verbatim in message history.
    categories=[x for x in ['중고명품','리셀','커머스','구매대행','유통','세무','보험','학원','퍼포먼스 마케팅','AI 자동화','교육','코칭'] if x in text]
    card=PersonaCard(name=(('·'.join(categories[:2]))+' 전문가 파트너' if categories else '전문가 파트너 캠페인'),industries=categories,
        pain_hypotheses=['개별 고객 조사로 확인할 가설: 현장 노하우를 AI 업무 시스템·교육으로 연결할 수 있는가'],
        search_queries=[f'{x} 전문가 대표 강의 협업 문의' for x in categories[:5]],
        unknowns=['개별 고객의 실제 문제·협업 의향','승인된 실적과 가격·수익분배','사용할 소개서 템플릿'])
    if os.getenv('ANTHROPIC_API_KEY') and os.getenv('SALES_AX_PERSONA_MODEL'):
        import anthropic
        response=anthropic.Anthropic().messages.create(model=os.environ['SALES_AX_PERSONA_MODEL'],max_tokens=2500,
            system='당신은 B2B 전문가 파트너 세일즈 설계자다. 고객 A는 협업할 현업 전문가, 고객 B는 최종 교육 고객이다. 공개 고객 사실을 생성하지 말고 모든 문제는 가설로 표시한다. 사용자 메시지를 PersonaCard 스키마에 맞는 JSON만으로 구조화하라. 스키마: '+json.dumps(PersonaCard.model_json_schema(),ensure_ascii=False),
            messages=[{'role':'user','content':text}])
        raw=''.join(b.text for b in response.content if b.type=='text').strip()
        if raw.startswith('```'):raw=raw.split('\n',1)[1].rsplit('```',1)[0]
        return PersonaCard.model_validate_json(raw),'AI_ANTHROPIC'
    return card,'RULE_BASED_MANUAL'


class PersonaService:
    def __init__(self,wf):self.wf=wf;self.db=wf.db;self.queue=JobQueue(self.db)

    def session(self,sid):
        row=self.db.one('SELECT * FROM persona_sessions WHERE id=?',(sid,))
        if not row:raise LookupError('대화가 없습니다.')
        latest=self.db.one('SELECT * FROM persona_versions WHERE session_id=? ORDER BY version DESC LIMIT 1',(sid,))
        return {**row,'messages':self.db.rows('SELECT role,content,created_at FROM persona_messages WHERE session_id=? ORDER BY id',(sid,)),
            'card':json.loads(latest['payload']) if latest else None,'provider':latest['provider'] if latest else None,
            'ai_connected':bool(os.getenv('ANTHROPIC_API_KEY') and os.getenv('SALES_AX_PERSONA_MODEL'))}

    def create(self):
        with self.db.transaction():sid=self.db.execute('INSERT INTO persona_sessions(title,created_at) VALUES(?,?)',('새 고객 정의',now())).lastrowid
        return self.session(sid)

    def save_card(self,sid,card,provider):
        self.session(sid)
        with self.db.transaction():
            row=self.db.one('SELECT version FROM persona_sessions WHERE id=?',(sid,));version=row['version']+1
            self.db.execute('INSERT INTO persona_versions(session_id,version,payload,provider,created_at) VALUES(?,?,?,?,?)',(sid,version,card.model_dump_json(),provider,now()))
            self.db.execute('UPDATE persona_sessions SET title=?,version=?,approved_version=NULL WHERE id=?',(card.name,version,sid))
            self.db.log('persona_updated',{'session_id':sid,'version':version,'provider':provider})
        return self.session(sid)

    def message(self,sid,text,key):
        self.session(sid)
        job=self.queue.enqueue('persona','persona:'+str(sid)+':'+key,{'session_id':sid,'text':text})
        def handle(payload):
            with self.db.transaction():self.db.execute('INSERT INTO persona_messages(session_id,role,content,created_at) VALUES(?,?,?,?)',(sid,'user',payload['text'],now()))
            history=self.db.rows("SELECT content FROM persona_messages WHERE session_id=? AND role='user' ORDER BY id",(sid,))
            card,provider=draft_persona('\n'.join(m['content'] for m in history))
            result=self.save_card(sid,card,provider)
            reply='고객 설명을 카드 초안으로 정리했습니다. 사실과 가설을 검토하고 수정한 뒤 승인하세요.' if provider=='AI_ANTHROPIC' else 'AI가 아직 연결되지 않아 규칙 기반 편집 초안을 준비했습니다. 원문은 보존했습니다. 카드를 수정·승인하면 고객 조사로 이어집니다.'
            with self.db.transaction():self.db.execute('INSERT INTO persona_messages(session_id,role,content,created_at) VALUES(?,?,?,?)',(sid,'assistant',reply,now()))
            return {'session_id':sid,'version':result['version']}
        job=self.queue.run(job['id'],handle)
        return {**self.session(sid),'job_id':job['id']}

    def approve(self,sid,version,confirmed):
        session=self.session(sid)
        if not confirmed or session['version']!=version or not session['card']:raise ValueError('현재 버전의 페르소나를 확인하고 승인하세요.')
        if session['campaign_id'] and session['approved_version']==version:return session
        card=PersonaCard.model_validate(session['card'])
        if not card.industries:raise ValueError('목표 업종을 하나 이상 정해주세요.')
        icp={k:getattr(card,k) for k in ('industries','buyer_titles','regions','disqualifiers')}
        icp.update(company_size='',pain_points=card.pain_hypotheses)
        with self.db.transaction():
            offer=self.db.execute('INSERT INTO offers(name,value_proposition) VALUES(?,?)',(card.product,card.value_prop)).lastrowid
            icp_id=self.db.execute('INSERT INTO icp_profiles(name,config) VALUES(?,?)',(card.name,json.dumps(icp,ensure_ascii=False))).lastrowid
            # Every approved revision creates a separate campaign: past contacts/messages remain immutable.
            cid=self.db.execute('INSERT INTO campaigns(name,motion,sender,icp,product,value_prop,booking_link,created_at,offer_id,icp_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (card.name,card.motion,'marketing@creatobi.com',json.dumps(icp,ensure_ascii=False),card.product,card.value_prop,'https://whattime.co.kr/campustrue/mobum_ai',now(),offer,icp_id)).lastrowid
            self.db.execute('UPDATE persona_sessions SET approved_version=?,campaign_id=? WHERE id=?',(version,cid,sid))
            self.db.log('persona_approved',{'session_id':sid,'version':version,'campaign_id':cid})
        return self.session(sid)
