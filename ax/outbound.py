"""200-person preparation and explicit approval-bound scheduling. No autonomous approval."""
import hashlib
import json
import os
import threading
from datetime import datetime,timezone
from .db import now
from .gmail import verify_raw


class Delivery:
    def __init__(self,wf):self.wf=wf;self.db=wf.db

    @property
    def enabled(self):return os.getenv('SALES_AX_SEND_ENABLED','false').lower()=='true'

    def schedule(self,did,version,recipient,scheduled_at,actor,confirmed):
        if not self.enabled:raise ValueError('예약 발송 기능이 서버에서 비활성화되어 있습니다.')
        when=datetime.fromisoformat(scheduled_at)
        if when.tzinfo is None or when<datetime.now(timezone.utc):raise ValueError('시간대가 있는 미래 예약 시각을 선택하세요.')
        with self.db.transaction():
            d=self.wf.draft(did);lead=self.wf.lead(d['lead_id'])
            if not confirmed or not actor.strip() or version!=d['version'] or d['approved_version']!=version or d['status']!='APPROVED':
                raise ValueError('수신자·최종 본문·첨부를 검수하고 현재 버전을 개별 승인해야 합니다.')
            if recipient!=lead['email'] or d['sync_state']!='VERIFIED':raise ValueError('검증된 Gmail 수신자와 일치하지 않습니다.')
            if self.wf.campaign(lead['campaign_id'])['motion']=='EXPERT_PARTNER' and not d['proposal_id']:
                raise ValueError('전문가 파트너십 메일에는 검수한 강사·파트너 제안서 PDF가 필요합니다.')
            if self.wf.qa(did):raise ValueError('QA 오류가 있습니다.')
            old=self.db.one('SELECT * FROM scheduled_deliveries WHERE draft_id=?',(did,))
            if old:return old
            sid=self.db.execute('INSERT INTO scheduled_deliveries(draft_id,version,recipient,scheduled_at,approved_by,created_at) VALUES(?,?,?,?,?,?)',
                (did,version,recipient,when.astimezone(timezone.utc).isoformat(),actor,now())).lastrowid
            self.db.log('delivery_scheduled',{'draft_id':did,'version':version,'scheduled_at':scheduled_at},lead['id'])
        return self.db.one('SELECT * FROM scheduled_deliveries WHERE id=?',(sid,))

    def dispatch(self,sid):
        if not self.enabled:return {'status':'DISABLED'}
        with self.db.lock:
            s=self.db.one('SELECT * FROM scheduled_deliveries WHERE id=?',(sid,))
            if not s or s['status']!='SCHEDULED':return s
            if datetime.fromisoformat(s['scheduled_at'])>datetime.now(timezone.utc):return s
            d=self.wf.draft(s['draft_id']);lead=self.wf.lead(d['lead_id']);campaign=self.wf.campaign(lead['campaign_id'])
            def hold(reason):
                with self.db.transaction():self.db.execute("UPDATE scheduled_deliveries SET status='HOLD',error=? WHERE id=?",(reason,sid))
                return {'status':'HOLD','reason':reason}
            if d['version']!=s['version'] or d['approved_version']!=s['version'] or d['status']!='APPROVED':return hold('승인 후 초안 변경됨')
            if self.wf.qa(d['id']):return hold('발송 직전 QA 차단')
            if lead['email']!=s['recipient']:return hold('수신자 변경됨')
            count=self.db.one("SELECT count(*) n FROM send_attempts WHERE status IN ('SENDING','SENT','UNCERTAIN') AND julianday(at)>julianday('now','-24 hours')")['n']
            if count>=min(200,int(os.getenv('SALES_AX_DAILY_SEND_LIMIT','200'))):return hold('연속 24시간 발송 상한 도달')
            client=self.wf.gmail_factory()
            if client.account().lower()!=campaign['sender'].lower():return hold('회사 발신 계정 불일치')
            p=self.db.one('SELECT * FROM proposals WHERE id=?',(d['proposal_id'],)) if d['proposal_id'] else None
            try:
                raw=client.read(d['gmail_draft_id'])
                verify_raw(raw,campaign['sender'],lead['email'],d['subject'],d['body'],p)
                if hashlib.sha256(raw.encode()).hexdigest()!=d['gmail_hash']:return hold('승인 후 Gmail 원본 변경됨')
            except Exception:return hold('Gmail 발송 직전 재검증 실패')
            with self.db.transaction():
                if self.db.one('SELECT id FROM send_attempts WHERE draft_id=? AND version=?',(d['id'],d['version'])):return hold('이미 발송을 시도한 버전')
                self.db.execute("INSERT INTO send_attempts(draft_id,version,status,at) VALUES(?,?,'SENDING',?)",(d['id'],d['version'],now()))
                self.db.execute("UPDATE scheduled_deliveries SET status='SENDING' WHERE id=?",(sid,))
            try:
                result=client.send_draft(d['gmail_draft_id'])
                with self.db.transaction():
                    self.wf.import_message(campaign['sender'],{'gmail_id':result['id'],'thread_id':result['threadId'],
                        'from':campaign['sender'],'to':lead['email'],'lead_id':lead['id'],'subject':d['subject'],'occurred_at':now()})
                    self.db.execute("UPDATE send_attempts SET status='SENT',gmail_id=?,thread_id=? WHERE draft_id=? AND version=?",(result['id'],result['threadId'],d['id'],d['version']))
                    self.db.execute("UPDATE drafts SET status='SENT' WHERE id=?",(d['id'],))
                    self.db.execute("UPDATE scheduled_deliveries SET status='SENT',gmail_id=?,thread_id=? WHERE id=?",(result['id'],result['threadId'],sid))
                    self.db.log('delivery_sent',{'draft_id':d['id'],'gmail_id':result['id'],'delivery_confirmed':False},lead['id'])
            except Exception:
                # API success followed by DB failure must never trigger automatic resend.
                with self.db.transaction():
                    self.db.execute("UPDATE scheduled_deliveries SET status='UNCERTAIN',error='발송 결과 확인 필요. 자동 재시도하지 않습니다.' WHERE id=?",(sid,))
                    self.db.execute("UPDATE send_attempts SET status='UNCERTAIN' WHERE draft_id=? AND version=?",(d['id'],d['version']))
            return self.db.one('SELECT * FROM scheduled_deliveries WHERE id=?',(sid,))

    def run_due(self):
        if not self.enabled:return
        for row in self.db.rows("SELECT id FROM scheduled_deliveries WHERE status='SCHEDULED' AND julianday(scheduled_at)<=julianday('now') ORDER BY scheduled_at LIMIT 20"):
            try:self.dispatch(row['id'])
            except Exception:pass # Durable SENDING/HOLD/UNCERTAIN remains visible; never retry the send blindly.


def register(app,wf):
    from fastapi import Query
    from pydantic import BaseModel,Field
    from .legacy import compose_body
    delivery=Delivery(wf);app.state.delivery=delivery

    @app.get('/api/candidates')
    def candidates(q:str='',limit:int=Query(default=200,ge=1,le=500)):
        from pathlib import Path
        path=Path(__file__).resolve().parents[1]/'data'/'candidate-index.json'
        if not path.exists():return {'state':'BLOCKED','candidates':[],'reason':'후보 데이터 소스가 연결되지 않았습니다. CSV를 가져올 수 있습니다.'}
        source=json.loads(path.read_text(encoding='utf-8'))
        rows=source['candidates'];rows=[r for r in rows if q.casefold() in (r['name']+' '+r['industry']+' '+r.get('notes','')).casefold()]
        return {'state':'MANUAL_IMPORT','provider':source['provider'],'observed_at':source['observed_at'],'total':len(rows),
            'listed_email_count':sum(bool(r['email']) for r in rows),'candidates':rows[:limit],
            'note':'실제 인덱스 조회 자료입니다. 적합성·공식 연락처·기존 접촉 검증 전이므로 발송 가능 고객으로 세지 않습니다.'}

    class ImportCandidates(BaseModel):
        campaign_id:int
        source_urls:list[str]=Field(min_length=1,max_length=200)

    @app.post('/api/candidates/import')
    def import_candidates(data:ImportCandidates):
        from pathlib import Path
        wf.campaign(data.campaign_id)
        path=Path(__file__).resolve().parents[1]/'data'/'candidate-index.json'
        if not path.exists():raise ValueError('후보 자료가 없습니다.')
        pool={p['source_url']:p for p in json.loads(path.read_text(encoding='utf-8'))['candidates']};results=[]
        for url in dict.fromkeys(data.source_urls):
            p=pool.get(url)
            if not p:results.append({'url':url,'status':'HOLD','reason':'등록된 후보 자료에 없는 출처'});continue
            old=wf.db.one("SELECT * FROM source_records WHERE provider='heepsy_index' AND external_id=?",(url,))
            if old:
                previous=wf.lead(old['lead_id'])
                with wf.db.transaction():
                    wf.db.execute('INSERT INTO leads(campaign_id,contact_id,created_at) VALUES(?,?,?) ON CONFLICT(campaign_id,contact_id) DO NOTHING',(data.campaign_id,previous['contact_id'],now()))
                    lead=wf.db.one('SELECT id FROM leads WHERE campaign_id=? AND contact_id=?',(data.campaign_id,previous['contact_id']))
                results.append({'url':url,'lead_id':lead['id'],'created':False});continue
            try:
                match=wf.db.one('SELECT id,name FROM contacts WHERE email=?',(p['email'].lower(),)) if p['email'] else None
                if match and match['name']!=p['name']:
                    results.append({'url':url,'status':'HOLD','reason':'다른 인물과 공유하는 이메일 가능성: 자동 병합하지 않음'});continue
                added=wf.add_lead({k:p.get(k,'') for k in ['name','company','email','source_url','title']}|{'campaign_id':data.campaign_id})
                lid=added['id']
                with wf.db.transaction():
                    wf.db.execute('INSERT INTO source_records(provider,external_id,lead_id,payload,imported_at) VALUES(?,?,?,?,?)',('heepsy_index',url,lid,json.dumps(p,ensure_ascii=False),now()))
                    wf.db.log('indexed_candidate_imported',{'provider':'heepsy_index','contact_verified':False},lid)
                results.append({'url':url,'lead_id':lid,'created':added['created']})
            except ValueError as exc:results.append({'url':url,'status':'HOLD','reason':str(exc)})
        return {'results':results,'source_modified':False,'verified':0,'sent':0}

    @app.get('/api/outbound')
    def overview():return {'enabled':delivery.enabled,'daily_limit':min(200,int(os.getenv('SALES_AX_DAILY_SEND_LIMIT','200'))),
        'scheduled':wf.db.rows('SELECT * FROM scheduled_deliveries ORDER BY scheduled_at'),'approval_required':True}

    class Schedule(BaseModel):
        version:int
        recipient:str
        scheduled_at:datetime
        actor:str=Field(min_length=1)
        confirmed:bool

    @app.post('/api/drafts/{did}/schedule')
    def schedule(did:int,data:Schedule):
        x=data.model_dump();x['scheduled_at']=data.scheduled_at.isoformat();return delivery.schedule(did,**x)

    class Batch(BaseModel):
        lead_ids:list[int]=Field(min_length=1,max_length=200)

    @app.post('/api/outbound/prepare')
    def prepare(data:Batch):
        results=[]
        for lid in dict.fromkeys(data.lead_ids):
            try:
                lead=wf.lead(lid);campaign=wf.campaign(lead['campaign_id'])
                facts=wf.db.rows('SELECT * FROM evidence WHERE contact_id=? AND human_checked=1 ORDER BY id DESC',(lead['contact_id'],))
                if not facts:raise ValueError('확인한 개인화 근거가 없어 보류합니다.')
                fact=facts[0]
                body=f"{lead['name']}님, 안녕하세요.\n\n{fact['claim']}에 관한 공개 자료를 바탕으로 연락드립니다.\n{fact['source_url']}\n\n{campaign['value_prop']}\n\n{campaign['product']}을 함께 검토하고 싶습니다. 구체적인 범위와 역할은 협의하여 정하는 제안입니다.\n\n15~20분 정도 이야기 나눌 수 있으실까요?\n{campaign['booking_link']}"
                d=wf.save_draft(lid,f"{lead['name']}님께 드리는 전문가 파트너십 제안",compose_body(campaign,body))
                results.append({'lead_id':lid,'draft_id':d['id'],'status':d['status']})
            except (ValueError,LookupError) as exc:results.append({'lead_id':lid,'status':'HOLD','reason':str(exc)})
        return {'results':results,'sent':0,'gmail_created':0,'note':'로컬 초안만 준비했습니다. Gmail 생성·개별 검수·승인은 별도 단계입니다.'}

    stopped=threading.Event()
    def loop():
        while not stopped.wait(15):delivery.run_due()
    @app.on_event('startup')
    def startup():
        if delivery.enabled:threading.Thread(target=loop,name='sales-ax-approved-delivery',daemon=True).start()
    @app.on_event('shutdown')
    def shutdown():stopped.set()
