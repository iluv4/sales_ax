import base64
import csv
import hashlib
import hmac
import io
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from .db import DB, now
from .service import Workflow, email_address, public_url
from .gmail import GmailDraftClient
from .legacy import campaign_config, research_prompt, compose_body
from .timeline import lead_timeline
from .sheet_import import read_csv, plan, import_snapshot, PIPELINE_COLUMNS, HISTORY_COLUMNS
from .analytics import funnel as cohort_funnel
from .seed import load as load_seed
from .oauth import hosted_store_ready

ROOT=Path(__file__).resolve().parents[1]


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Campaign(Input):
    name:str=Field(min_length=1,max_length=200)
    motion:Literal['EXPERT_PARTNER','CORPORATE_AX','EDU_ALLIANCE','CREATOR_PARTNER']='EXPERT_PARTNER'
    sender:str='marketing@creatobi.com'
    product:str=Field(min_length=1)
    value_prop:str=Field(min_length=1)
    industries:list[str]=[]
    regions:list[str]=[]
    buyer_titles:list[str]=[]
    company_size:str=''
    pain_points:list[str]=[]
    disqualifiers:list[str]=[]
    booking_link:str='https://whattime.co.kr/campustrue/mobum_ai'
    target_daily:int=Field(default=200,ge=1,le=10000)


class Lead(Input):
    campaign_id:int
    name:str=Field(min_length=1,max_length=200)
    company:str=Field(min_length=1,max_length=200)
    title:str=''
    domain:str=''
    email:str=''
    source_url:str=''


class Verify(Input):
    confirmed:bool
    source_url:str


class Evidence(Input):
    claim:str=Field(min_length=1)
    source_url:str
    human_checked:bool=False
    confidence:float=Field(default=.5,ge=0,le=1)


class Qualify(Input):
    score:int=Field(ge=0,le=100)
    fit:Literal['strong','moderate','weak','disqualified']
    rationale:str=Field(min_length=1)


class Draft(Input):
    subject:str=Field(min_length=1,max_length=998)
    body:str=Field(min_length=1,max_length=50000)
    proposal_id:int|None=None
    step:int=Field(default=0,ge=0,le=10)


class Approval(Input):
    version:int
    actor:str
    confirmed:bool


class Meeting(Input):
    lead_id:int
    starts_at:datetime
    external_id:str|None=None


class Completion(Input):
    actor:str
    confirmed:bool
    notes:str
    next_step:str=''


class Message(Input):
    model_config=ConfigDict(extra='forbid',populate_by_name=True)
    account:str
    gmail_id:str=Field(min_length=1)
    thread_id:str=Field(min_length=1)
    rfc_id:str=''
    in_reply_to:str=''
    from_address:str=Field(alias='from')
    to:str
    lead_id:int|None=None
    subject:str=''
    snippet:str=''
    occurred_at:datetime
    intent:Literal['UNCLASSIFIED','AUTO_REPLY','BOUNCE','HUMAN','POSITIVE','QUESTION','REJECTED','OPT_OUT']='UNCLASSIFIED'


class Deal(Input):
    stage:Literal['DISCOVERY','NEGOTIATION','CONTRACTED','PAID','LOST']
    amount_krw:int=Field(ge=0)
    evidence:str=Field(min_length=1)
    actor:str=Field(min_length=1)
    confirmed:bool


def create_app(db_path=None,gmail_factory=None):
    app=FastAPI(title='모범AI Sales AX',version='0.1.0')
    db=DB(db_path or os.getenv('SALES_AX_DB',str(ROOT/'data'/'sales-ax.db')))
    wf=Workflow(db,gmail_factory or GmailDraftClient.configured)
    app.state.workflow=wf
    # Hosted demo: one extra allowed host, every request behind a shared password; Google OAuth only with persistent encrypted storage.
    public_host=os.getenv('SALES_AX_PUBLIC_HOST','').strip().lower()
    demo_password=os.getenv('SALES_AX_DEMO_PASSWORD','')
    if public_host and len(demo_password)<12:raise RuntimeError('공개 데모에는 12자 이상의 SALES_AX_DEMO_PASSWORD가 필요합니다.')
    origins={'http://127.0.0.1:8765','http://localhost:8765'}|({'https://'+public_host} if public_host else set())

    def demo_authorized(header):
        scheme,_,value=header.partition(' ')
        if scheme.lower()!='basic':return False
        try:password=base64.b64decode(value,validate=True).decode().partition(':')[2]
        except (ValueError,UnicodeError):return False
        return hmac.compare_digest(password.encode(),demo_password.encode())

    @app.middleware('http')
    async def local_guard(request:Request,call_next):
        # Local single-operator MVP: loopback binding, host/origin validation, no cross-site mutation.
        host=request.headers.get('host','').split(':')[0].lower()
        if host not in ('127.0.0.1','localhost','testserver') and not (public_host and host==public_host):
            return JSONResponse({'detail':'로컬 호스트만 허용합니다.'},status_code=403)
        if public_host:
            if not demo_authorized(request.headers.get('authorization','')):
                return JSONResponse({'detail':'데모 비밀번호가 필요합니다.'},status_code=401,headers={'WWW-Authenticate':'Basic realm="Sales AX demo", charset="UTF-8"'})
            if request.url.path.startswith('/api/integrations/google') and not hosted_store_ready():
                return JSONResponse({'detail':'공개 데모의 Google 연결 저장소(Blob·암호화 키)가 설정되지 않았습니다.'},status_code=403)
        origin=request.headers.get('origin')
        if request.method not in ('GET','HEAD','OPTIONS') and origin and origin not in origins:
            return JSONResponse({'detail':'외부 출처 요청 차단'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(ValueError)
    async def value_error(request,exc): return JSONResponse({'detail':str(exc)},status_code=400)

    @app.exception_handler(LookupError)
    async def lookup_error(request,exc): return JSONResponse({'detail':str(exc)},status_code=404)

    @app.get('/api/status')
    def status():
        return {'send_enabled':False,'mode':'HOSTED_DEMO' if public_host else 'LOCAL_SINGLE_OPERATOR','gmail_configured':bool(os.getenv('GMAIL_AUTHORIZED_USER_FILE') or os.getenv('GOOGLE_APPLICATION_CREDENTIALS')),
                'ai_configured':bool(os.getenv('ANTHROPIC_API_KEY')),'ai_provider':'legacy-anthropic (optional adapter)',
                'upstreams':{'sales':'670448c013ecfdd0629dafbcf9a2cc1c05398e7f','sales_saas_prompt':'674375f069aba1c00a16678afbef6b473a989dee'}}

    @app.get('/api/campaigns')
    def campaigns(): return db.rows('SELECT * FROM campaigns ORDER BY id DESC')

    @app.post('/api/campaigns')
    def new_campaign(data:Campaign):
        if not email_address(data.sender): raise ValueError('발신 이메일이 필요합니다.')
        public_url(data.booking_link)
        icp={k:getattr(data,k) for k in ('industries','regions','buyer_titles','company_size','pain_points','disqualifiers')}
        # Validate using the upstream ICP object before storing canonical JSON.
        with db.transaction():
            cid=db.execute('INSERT INTO campaigns(name,motion,sender,icp,product,value_prop,booking_link,target_daily,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (data.name,data.motion,email_address(data.sender),json.dumps(icp,ensure_ascii=False),data.product,data.value_prop,data.booking_link,data.target_daily,now())).lastrowid
            campaign_config(wf.campaign(cid))
            db.log('campaign_created',{'id':cid})
        return wf.campaign(cid)

    @app.get('/api/leads')
    def leads(q:str='',campaign_id:int|None=None):
        result=[wf.lead(r['id']) for r in db.rows('SELECT id FROM leads ORDER BY id DESC')]
        return [r for r in result if (campaign_id is None or r['campaign_id']==campaign_id) and q.casefold() in ' '.join(str(r.get(k) or '') for k in ('name','company','email','stage')).casefold()]

    @app.post('/api/leads')
    def new_lead(data:Lead): return wf.add_lead(data.model_dump())

    @app.post('/api/leads/import')
    async def import_leads(campaign_id:int=Form(...),file:UploadFile=File(...)):
        content=await file.read(5*1024*1024+1)
        if len(content)>5*1024*1024: raise ValueError('CSV는 5MB 이하이어야 합니다.')
        try: rows=list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
        except UnicodeError: raise ValueError('UTF-8 CSV를 사용하세요.')
        results=[]
        for index,row in enumerate(rows[:10000],start=2):
            try:
                data=Lead(campaign_id=campaign_id,**{k:v or '' for k,v in row.items() if k in Lead.model_fields and k!='campaign_id'})
                results.append({'row':index,**wf.add_lead(data.model_dump())})
            except (ValueError,LookupError) as exc: results.append({'row':index,'error':str(exc)})
        with db.transaction(): db.log('csv_import',{'rows':len(results),'source_filename':file.filename})
        return {'results':results,'source_modified':False}

    @app.get('/api/leads/{lid}')
    def lead_detail(lid:int):
        lead=wf.lead(lid)
        return {'lead':lead,'evidence':db.rows('SELECT * FROM evidence WHERE contact_id=?',(lead['contact_id'],)),
            'drafts':db.rows('SELECT * FROM drafts WHERE lead_id=? ORDER BY step',(lid,)),
            'proposals':db.rows('SELECT id,filename,sha256,identity_checked,created_at FROM proposals WHERE lead_id=?',(lid,)),
            'messages':db.rows('SELECT * FROM messages WHERE lead_id=? ORDER BY occurred_at DESC',(lid,)),
            'meetings':db.rows('SELECT * FROM meetings WHERE lead_id=? ORDER BY starts_at DESC',(lid,)),
            'tasks':db.rows('SELECT * FROM tasks WHERE lead_id=?',(lid,)),
            'deal':db.one('SELECT * FROM deals WHERE lead_id=?',(lid,)),
            'timeline':lead_timeline(wf,lid),
            'audit':db.rows('SELECT * FROM audit_logs WHERE lead_id=? ORDER BY id DESC',(lid,))}

    @app.get('/api/leads/{lid}/timeline')
    def timeline(lid:int): return lead_timeline(wf,lid)

    @app.post('/api/imports/sheet-snapshot')
    async def sheet_snapshot(campaign_id:int=Form(...),confirm:bool=Form(False),pipeline:UploadFile=File(...),history:UploadFile|None=File(None)):
        def text(f):
            content=f.file.read(5*1024*1024+1)
            if len(content)>5*1024*1024: raise ValueError('CSV는 5MB 이하이어야 합니다.')
            try: return content.decode('utf-8-sig')
            except UnicodeError: raise ValueError('UTF-8 CSV를 사용하세요.')
        snapshot={'pipeline':read_csv(text(pipeline),PIPELINE_COLUMNS),'history':read_csv(text(history),HISTORY_COLUMNS) if history and history.filename else []}
        wf.campaign(campaign_id)
        return import_snapshot(wf,snapshot,campaign_id) if confirm else plan(wf,snapshot)

    @app.post('/api/leads/{lid}/verify')
    def verify(lid:int,data:Verify):
        wf.verify_contact(lid,data.confirmed,data.source_url); return wf.lead(lid)

    @app.post('/api/leads/{lid}/qualify')
    def qualify(lid:int,data:Qualify): return wf.qualify(lid,**data.model_dump())

    @app.post('/api/leads/{lid}/evidence')
    def evidence(lid:int,data:Evidence):
        lead=wf.lead(lid)
        if not public_url(data.source_url): raise ValueError('근거 URL이 필요합니다.')
        with db.transaction():
            db.execute('INSERT INTO evidence(contact_id,claim,source_url,observed_at,human_checked,confidence) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(contact_id,claim,source_url) DO UPDATE SET human_checked=excluded.human_checked,confidence=excluded.confidence',
                (lead['contact_id'],data.claim,data.source_url,now(),int(data.human_checked),data.confidence))
            db.log('evidence_recorded',data.model_dump(),lid)
        return {'ok':True}

    @app.post('/api/leads/{lid}/suppress')
    def suppress(lid:int,data:Completion):
        if not data.confirmed or not data.notes.strip(): raise ValueError('수신거부 확인과 사유가 필요합니다.')
        wf.suppress(lid,data.notes); return {'ok':True}

    @app.post('/api/leads/{lid}/research')
    def research(lid:int):
        lead=wf.lead(lid); wf.active(lead)
        if not os.getenv('ANTHROPIC_API_KEY'): raise HTTPException(503,'리서치 어댑터 키가 없습니다. 근거 URL을 수동 등록할 수 있습니다.')
        # Pinned sales agent runs against a temporary scratch DB, never the canonical DB.
        from sales_agent.agent import SalesAgent
        from sales_agent.store import Store
        config=campaign_config(wf.campaign(lead['campaign_id']))
        config.agent.model=os.getenv('SALES_AX_RESEARCH_MODEL',config.agent.model)
        store=Store()
        try:
            scratch=store.add_lead(lead['company'],contact_name=lead['name'],email=lead['email'] or '',website=lead['source_url'])
            result=SalesAgent(config,store).send(f'Research lead #{scratch}. Record public source URLs and separate verified facts from hypotheses. Do not send anything. Provide Korean findings.').text
        except Exception:
            with db.transaction(): db.log('research_failed',{'reason':'provider request failed'},lid)
            raise HTTPException(502,'리서치 제공자 요청 실패. API 설정과 모델 지원 여부를 확인하세요.')
        finally: store.close()
        with db.transaction():
            db.execute('UPDATE leads SET research=? WHERE id=?',(result,lid))
            db.log('research_suggested',{'human_verified':False},lid)
        return {'research':result,'human_verified':False}

    @app.get('/api/campaigns/{cid}/research-prompt')
    def prompt(cid:int): return {'prompt':research_prompt(wf.campaign(cid))}

    @app.post('/api/leads/{lid}/proposal')
    async def upload_pdf(lid:int,file:UploadFile=File(...),identity_checked:bool=Form(False)):
        content=await file.read(20*1024*1024+1)
        return {'id':wf.attach(lid,file.filename or 'proposal.pdf',content,identity_checked)}

    @app.get('/api/proposals/{pid}/pdf')
    def pdf(pid:int):
        proposal=db.one('SELECT content FROM proposals WHERE id=?',(pid,))
        if not proposal: raise LookupError('PDF가 없습니다.')
        return Response(bytes(proposal['content']),media_type='application/pdf')

    @app.post('/api/leads/{lid}/draft')
    def draft(lid:int,data:Draft): return wf.save_draft(lid,**data.model_dump())

    @app.post('/api/leads/{lid}/suggest-draft')
    def suggest(lid:int):
        lead=wf.lead(lid); wf.active(lead); campaign=wf.campaign(lead['campaign_id'])
        facts=db.rows('SELECT * FROM evidence WHERE contact_id=? AND human_checked=1 ORDER BY id DESC',(lead['contact_id'],))
        if not facts: raise ValueError('사람이 확인한 공개 근거를 먼저 등록하세요.')
        fact=facts[0]
        subject=f"{lead['name']}님의 {fact['claim'][:50]} 관련 협업 제안"
        body=f"{lead['name']}님, 안녕하세요.\n\n{fact['claim']}에 관한 공개 자료를 바탕으로 연락드립니다.\n{fact['source_url']}\n\n{campaign['value_prop']}\n\n{campaign['product']}에 관해 함께 논의하고 싶습니다. 현장 판단 기준을 AI 시스템과 연결하는 방향을 제안드립니다.\n\n15~20분 정도 파트너 미팅이 가능하실까요?\n{campaign['booking_link']}"
        return {'subject':subject,'body':compose_body(campaign,body),'generated_by':'evidence_template','claims_require_review':True}

    @app.post('/api/drafts/{did}/qa')
    def qa(did:int): return {'errors':wf.qa(did)}

    @app.post('/api/drafts/{did}/gmail')
    def sync_draft(did:int):
        try: return wf.sync_draft(did)
        except ValueError: raise
        except Exception: raise HTTPException(503,'Gmail OAuth 설정이 필요하거나 계정 조회에 실패했습니다.')

    @app.post('/api/drafts/{did}/approve')
    def approve(did:int,data:Approval): return wf.approve(did,**data.model_dump())

    @app.post('/api/drafts/{did}/send')
    def send(did:int):
        # No send adapter exists. Approval records a review; it does not initiate delivery.
        raise HTTPException(403,'현재 릴리스는 발송 비활성화 상태입니다. 초안 생성과 검수만 가능합니다.')

    @app.post('/api/messages/import')
    def import_message(data:Message):
        item=data.model_dump(by_alias=True); account=item.pop('account')
        item['occurred_at']=data.occurred_at.isoformat()
        if data.occurred_at.tzinfo is None: raise ValueError('시간대가 필요합니다.')
        return wf.import_message(account,item)

    @app.post('/api/sync/gmail')
    def sync_gmail():
        try:
            client=wf.gmail_factory(); account=client.account(); created=0; unmatched=0
            threads=db.rows('SELECT DISTINCT thread_id FROM messages WHERE account=?',(account,))
            for thread in threads:
                for msg in client.thread_messages(thread['thread_id']):
                    if 'DRAFT' in msg.get('labelIds',[]): continue
                    headers={h['name'].lower():h['value'] for h in msg.get('payload',{}).get('headers',[])}
                    item={'gmail_id':msg['id'],'thread_id':msg['threadId'],'from':headers.get('from',''),
                          'to':headers.get('to',''),'rfc_id':headers.get('message-id',''),'in_reply_to':headers.get('in-reply-to',''),
                          'subject':headers.get('subject',''),'snippet':msg.get('snippet',''),
                          'occurred_at':datetime.fromtimestamp(int(msg['internalDate'])/1000,__import__('datetime').timezone.utc).isoformat(),
                          'intent':'AUTO_REPLY' if headers.get('auto-submitted','no')!='no' else ('BOUNCE' if 'mailer-daemon' in headers.get('from','').lower() else 'UNCLASSIFIED')}
                    try: created+=int(wf.import_message(account,item)['created'])
                    except ValueError: unmatched+=1
            with db.transaction(): db.execute('INSERT INTO sync_runs(source,status,detail,at) VALUES(?,?,?,?)',('gmail','PARTIAL' if unmatched else 'SUCCESS',json.dumps({'created':created,'unmatched':unmatched,'threads':len(threads)}),now()))
            return {'created':created,'unmatched':unmatched,'threads':len(threads),'note':'기존에 매칭된 스레드만 읽습니다. 최초 발신 기록은 메시지 가져오기로 연결하세요.'}
        except Exception:
            with db.transaction(): db.execute('INSERT INTO sync_runs(source,status,detail,at) VALUES(?,?,?,?)',('gmail','FAILED','계정 또는 Gmail 조회 실패',now()))
            raise HTTPException(503,'Gmail 동기화 실패. 실행 로그를 확인하세요.')

    @app.get('/api/meetings')
    def meetings(): return db.rows('SELECT m.*,c.name,o.name company FROM meetings m JOIN leads l ON l.id=m.lead_id JOIN contacts c ON c.id=l.contact_id LEFT JOIN organizations o ON o.id=c.org_id ORDER BY starts_at')

    @app.post('/api/meetings')
    def book(data:Meeting):
        wf.lead(data.lead_id)
        if data.starts_at.tzinfo is None: raise ValueError('미팅 시간대가 필요합니다.')
        with db.transaction():
            if data.external_id:
                old=db.one('SELECT * FROM meetings WHERE external_id=?',(data.external_id,))
                if old:
                    if old['lead_id']!=data.lead_id: raise ValueError('다른 고객의 캘린더 이벤트입니다.')
                    return old
            mid=db.execute('INSERT INTO meetings(lead_id,external_id,starts_at) VALUES(?,?,?)',(data.lead_id,data.external_id,data.starts_at.isoformat())).lastrowid
            db.execute("UPDATE leads SET stage='MEETING_BOOKED' WHERE id=? AND stage NOT IN ('WON','LOST')",(data.lead_id,))
            db.execute("UPDATE tasks SET status='canceled' WHERE lead_id=? AND kind IN ('followup_email','call')",(data.lead_id,))
            db.log('meeting_booked',{'meeting_id':mid},data.lead_id)
        return db.one('SELECT * FROM meetings WHERE id=?',(mid,))

    @app.post('/api/meetings/{mid}/complete')
    def complete(mid:int,data:Completion):
        wf.complete_meeting(mid,**data.model_dump()); return {'ok':True}

    @app.post('/api/leads/{lid}/deal')
    def deal(lid:int,data:Deal):
        wf.lead(lid)
        if not data.confirmed: raise ValueError('계약·매출은 담당자 확인이 필요합니다.')
        with db.transaction():
            db.execute('INSERT INTO deals(lead_id,stage,amount_krw,evidence,confirmed_by,verified_at) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(lead_id) DO UPDATE SET stage=excluded.stage,amount_krw=excluded.amount_krw,evidence=excluded.evidence,confirmed_by=excluded.confirmed_by,verified_at=excluded.verified_at',
                (lid,data.stage,data.amount_krw,data.evidence,data.actor,now()))
            db.log('deal_manually_verified',data.model_dump(),lid)
        return {'ok':True}

    @app.get('/api/drafts')
    def drafts(): return db.rows('SELECT d.*,c.name,c.email,o.name company FROM drafts d JOIN leads l ON l.id=d.lead_id JOIN contacts c ON c.id=l.contact_id LEFT JOIN organizations o ON o.id=c.org_id ORDER BY d.id DESC')

    @app.get('/api/tasks')
    def tasks(): return db.rows('SELECT t.*,c.name FROM tasks t JOIN leads l ON l.id=t.lead_id JOIN contacts c ON c.id=l.contact_id ORDER BY due_date')

    @app.get('/api/messages')
    def messages(): return db.rows('SELECT m.*,c.name FROM messages m JOIN leads l ON l.id=m.lead_id JOIN contacts c ON c.id=l.contact_id ORDER BY occurred_at DESC')

    @app.get('/api/analytics/funnel')
    def funnel(campaign_id:int|None=None,industry:str='',start:str='',end:str=''):
        # Filters use the first-real-send cohort; without filters keep the whole-DB view.
        if campaign_id is None and not industry and not start and not end: return wf.funnel()
        return cohort_funnel(db,campaign_id,industry or None,start or None,end or None)

    @app.get('/api/audit-logs')
    def audits(): return db.rows('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 500')

    @app.get('/api/sync-runs')
    def runs(): return db.rows('SELECT * FROM sync_runs ORDER BY id DESC LIMIT 100')

    @app.get('/api/templates')
    def templates(): return db.rows('SELECT id,name,filename,sha256,mappings,created_at FROM templates')

    @app.post('/api/templates')
    async def template(name:str=Form(...),mappings:str=Form('{}'),file:UploadFile=File(...)):
        content=await file.read(30*1024*1024+1)
        if len(content)>30*1024*1024 or not (file.filename or '').lower().endswith('.pptx'): raise ValueError('30MB 이하 원본 PPTX를 등록하세요.')
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if 'ppt/presentation.xml' not in z.namelist(): raise ValueError()
            mapping=json.loads(mappings)
            if not isinstance(mapping,dict): raise ValueError()
        except Exception: raise ValueError('유효한 PPTX와 JSON 변수맵이 필요합니다.')
        with db.transaction():
            tid=db.execute('INSERT INTO templates(name,filename,content,sha256,mappings,created_at) VALUES(?,?,?,?,?,?)',
                (name,file.filename,content,hashlib.sha256(content).hexdigest(),json.dumps(mapping,ensure_ascii=False),now())).lastrowid
            db.log('template_registered',{'id':tid,'rendering_enabled':False})
        return {'id':tid,'rendering_enabled':False}

    from .integrations import register
    register(app,wf,custom_gmail=gmail_factory is not None)
    from .persona_api import register as register_personas
    register_personas(app,wf)
    from .outbound import register as register_outbound
    register_outbound(app,wf)

    @app.get('/')
    def index(): return FileResponse(ROOT/'web'/'index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static')

    # Serverless storage resets on cold start: reload the sheet snapshot kept in an encrypted env var (never in git).
    seed=os.getenv('SALES_AX_SEED_GZ_B64','')
    if seed and not db.one('SELECT id FROM campaigns LIMIT 1'):
        load_seed(wf,lambda name,product,value_prop:new_campaign(Campaign(name=name,product=product,value_prop=value_prop))['id'],seed)
    return app


app=create_app()
