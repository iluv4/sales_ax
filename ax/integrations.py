import json
import os
from pathlib import Path
from datetime import datetime,timezone
from fastapi import HTTPException,UploadFile,File,Request
from fastapi.responses import RedirectResponse,HTMLResponse
from pydantic import BaseModel,Field
from .db import now
from .oauth import GoogleConnection
from .gmail import GmailDraftClient
from .documents import FeatPaperManualProvider,FeatPaperCsvProvider
from .google_readonly import sync_calendar
from .analytics import documents


def register(app,wf,custom_gmail=False):
    db=wf.db
    directory=Path(os.getenv('SALES_AX_PRIVATE_DIR',str(Path(__file__).resolve().parents[1]/'data'/'private')))
    google=GoogleConnection(directory);app.state.google=google
    if not custom_gmail:
        def factory():
            if google.token_path.exists():
                from googleapiclient.discovery import build
                return GmailDraftClient(build('gmail','v1',credentials=google.credentials(),cache_discovery=False))
            return GmailDraftClient.configured()
        wf.gmail_factory=factory

    def record(provider,state,detail):
        with db.transaction():db.execute('INSERT OR REPLACE INTO integration_health(provider,state,detail,checked_at) VALUES(?,?,?,?)',(provider,state,detail,now()))

    @app.get('/api/integrations/health')
    def health():
        latest={r['provider']:r for r in db.rows('SELECT * FROM integration_health')}
        connection=google.status()
        return {'google':connection,'providers':[
            latest.get('gmail',{'provider':'gmail','state':'BLOCKED','detail':'로컬 OAuth 연결·실제 호출 미검증','checked_at':None}),
            latest.get('calendar',{'provider':'calendar','state':'BLOCKED','detail':'읽기 전용 OAuth 연결·실제 호출 미검증','checked_at':None}),
            latest.get('sheets',{'provider':'sheets','state':'BLOCKED','detail':'원본 읽기 전용 가져오기 대기','checked_at':None}),
            {'provider':'featpaper','state':'MANUAL','detail':'수신자별 링크 직접 등록 · API 자동 추적 없음'},
            {'provider':'proposal_render','state':'BLOCKED','detail':'template_missing · PDF 직접 등록 가능'}],
            'send_enabled':False,'mode':'LOCAL_SINGLE_OPERATOR','codex_connector_is_separate':True}

    @app.post('/api/integrations/google/client')
    async def configure(file:UploadFile=File(...)):
        payload=await file.read(64*1024+1)
        if len(payload)>64*1024:raise ValueError('OAuth JSON 파일이 너무 큽니다.')
        try:return google.configure(payload)
        except (UnicodeError,json.JSONDecodeError):raise ValueError('유효한 OAuth JSON이 필요합니다.')

    @app.post('/api/integrations/google/start')
    def start():
        url,secret=google.begin()
        response=__import__('fastapi').responses.JSONResponse({'authorization_url':url})
        response.set_cookie('sales_ax_oauth',secret,httponly=True,samesite='lax',max_age=600,path='/api/integrations/google')
        return response

    @app.get('/api/integrations/google/callback')
    def callback(request:Request,state:str='',code:str='',error:str=''):
        try:
            if error or not code:raise ValueError('Google 연결을 완료하지 않았습니다. 다시 시작할 수 있습니다.')
            account=google.finish(state,code,request.cookies.get('sales_ax_oauth',''))
            record('gmail','REAL','OAuth 계정 확인 및 Gmail 프로필 읽기 성공')
            result=RedirectResponse('/#settings',status_code=303)
        except Exception as exc:
            record('google','BLOCKED','OAuth 완료 실패 · 토큰 또는 인증 코드를 로그에 기록하지 않음')
            # Do not return provider responses, URLs, codes, or secrets.
            result=HTMLResponse('<h1>Google 연결을 완료하지 못했습니다.</h1><p>회사 계정·OAuth 동의·리디렉션 설정을 확인하고 다시 연결하세요.</p><a href="/#settings">연동 화면으로 돌아가기</a>',status_code=400)
        result.delete_cookie('sales_ax_oauth',path='/api/integrations/google')
        return result

    @app.post('/api/integrations/google/check')
    def check():
        results={}
        try:
            client=wf.gmail_factory();account=client.account()
            if account.lower()!=os.getenv('SALES_AX_GOOGLE_ACCOUNT','marketing@creatobi.com').lower():raise ValueError()
            record('gmail','REAL','회사 Gmail 프로필 조회 성공');results['gmail']='REAL'
        except Exception:record('gmail','BLOCKED','인증·프로필 확인 실패');results['gmail']='BLOCKED'
        try:
            from googleapiclient.discovery import build
            service=build('calendar','v3',credentials=google.credentials(),cache_discovery=False)
            service.calendars().get(calendarId='primary').execute()
            record('calendar','REAL','기본 Calendar 읽기 성공');results['calendar']='REAL'
        except Exception:record('calendar','BLOCKED','Calendar 읽기 권한·OAuth 확인 필요');results['calendar']='BLOCKED'
        return {'results':results,'send_enabled':False}

    @app.post('/api/integrations/calendar/sync')
    def calendar_sync():
        try:
            from googleapiclient.discovery import build
            service=build('calendar','v3',credentials=google.credentials(),cache_discovery=False)
            result=sync_calendar(wf,service)
            record('calendar','REAL','일정 읽기 전용 동기화 성공');return result
        except Exception:
            record('calendar','BLOCKED','동기화 실패 · OAuth/권한을 확인하세요.')
            raise HTTPException(503,'Calendar 동기화 실패. 실제 완료·계약·매출은 변경하지 않았습니다.')

    @app.get('/api/integrations/calendar/unmatched')
    def unmatched():return db.rows("SELECT id,external_id,payload,imported_at FROM source_records WHERE provider='calendar_unmatched' AND lead_id IS NULL ORDER BY imported_at DESC")

    @app.post('/api/integrations/gmail/bootstrap')
    def bootstrap():
        from email.utils import parseaddr
        try:client=wf.gmail_factory();account=client.account()
        except Exception:raise HTTPException(503,'Gmail OAuth 연결을 먼저 완료하세요.')
        expected=os.getenv('SALES_AX_GOOGLE_ACCOUNT','marketing@creatobi.com')
        if account.lower()!=expected.lower():raise ValueError('회사 발신 계정 불일치')
        created=0;ambiguous=0;failed=0
        contacts=db.rows('SELECT DISTINCT c.id,c.email FROM contacts c JOIN leads l ON l.contact_id=c.id WHERE c.email IS NOT NULL')
        for contact in contacts:
            leads=db.rows('SELECT id FROM leads WHERE contact_id=?',(contact['id'],))
            if len(leads)!=1:ambiguous+=1;continue
            try:
                for msg in client.sent_for(contact['email']):
                    if 'SENT' not in msg.get('labelIds',[]):continue
                    headers={h['name'].lower():h['value'] for h in msg.get('payload',{}).get('headers',[])}
                    if headers.get('cc') or headers.get('bcc'):ambiguous+=1;continue
                    if parseaddr(headers.get('to',''))[1].lower()!=contact['email']:ambiguous+=1;continue
                    item={'gmail_id':msg['id'],'thread_id':msg['threadId'],'from':headers.get('from',''),
                        'to':headers.get('to',''),'rfc_id':headers.get('message-id',''),'subject':headers.get('subject',''),
                        'occurred_at':datetime.fromtimestamp(int(msg['internalDate'])/1000,timezone.utc).isoformat(),
                        'lead_id':leads[0]['id']}
                    existing=db.one('SELECT id,lead_id FROM messages WHERE account=? AND gmail_id=?',(account,msg['id']))
                    if existing and existing['lead_id']==leads[0]['id']:
                        with db.transaction():db.execute("UPDATE messages SET thread_id=?,rfc_id=?,source='gmail_api' WHERE id=?",(msg['threadId'],headers.get('message-id',''),existing['id']))
                    else:created+=int(wf.import_message(account,item)['created'])
            except Exception:failed+=1
        state='REAL' if not failed else 'BLOCKED'
        record('gmail',state,f'실제 SENT 조회: 새 기록 {created}, 수동 매칭 필요 {ambiguous}, 실패 {failed}')
        return {'created':created,'ambiguous':ambiguous,'failed':failed,'source':'gmail_api','send_enabled':False}

    class Link(BaseModel):
        proposal_id:int
        lead_id:int
        url:str
        external_doc_id:str|None=None

    @app.get('/api/document-links')
    def links(lead_id:int|None=None):
        if lead_id is not None:return db.rows('SELECT * FROM document_links WHERE lead_id=?',(lead_id,))
        return db.rows('SELECT * FROM document_links')

    @app.post('/api/document-links')
    def attach(data:Link):return FeatPaperManualProvider(db).attach_link(**data.model_dump())

    class Observation(BaseModel):
        link_id:int
        confirmed:bool
        observation_source:str
        occurred_at:datetime
        external_event_id:str=Field(min_length=1)
        is_mock:bool=False

    @app.post('/api/document-events/observe')
    def observe(data:Observation):
        payload=data.model_dump();payload['occurred_at']=data.occurred_at.isoformat();payload['event_type']='view_observed'
        return FeatPaperManualProvider(db).import_events(payload)

    @app.get('/api/analytics/documents')
    def document_analytics(campaign_id:int|None=None,demo:bool=False):return documents(db,campaign_id,demo)

    class Classification(BaseModel):
        intent:__import__('typing').Literal['HUMAN','POSITIVE','QUESTION','REJECTED','OPT_OUT','AUTO_REPLY','BOUNCE']
        actor:str=Field(min_length=1)
        confirmed:bool

    @app.post('/api/messages/{mid}/classify')
    def classify(mid:int,data:Classification):
        message=db.one('SELECT * FROM messages WHERE id=?',(mid,))
        if not message:raise LookupError('메시지가 없습니다.')
        if not data.confirmed or message['direction']!='IN':raise ValueError('사람 확인은 수신 메시지에만 적용할 수 있습니다.')
        with db.transaction():
            db.execute('UPDATE messages SET intent=?,classified_by=?,classification_confidence=1 WHERE id=?',(data.intent,data.actor,mid))
            db.log('reply_classified',{'message_id':mid,'intent':data.intent},message['lead_id'])
        if data.intent in ('OPT_OUT','REJECTED','BOUNCE'):wf.suppress(message['lead_id'],data.intent)
        return {'ok':True}
