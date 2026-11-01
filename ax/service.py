import hashlib
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
from email.utils import parseaddr
from pypdf import PdfReader
from .db import now
from .legacy import qualification, compose_body, schedule_next_touch
from .gmail import mime_message, verify_raw


def norm(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


def email_address(value):
    value = norm(value)
    if value and not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', value):
        raise ValueError('업무용 이메일 주소 한 개를 입력하세요.')
    return value or None


def public_url(value):
    if value and (urlparse(value).scheme not in ('http', 'https') or not urlparse(value).hostname):
        raise ValueError('출처는 http(s) URL이어야 합니다.')
    return value


class Workflow:
    def __init__(self, db, gmail_factory=None):
        self.db = db
        self.gmail_factory = gmail_factory

    def lead(self, lid):
        row = self.db.one('SELECT l.*, c.name,c.title,c.email,c.verified,c.suppressed,c.suppression_reason,c.source_url, '
                          'o.name company,o.domain FROM leads l JOIN contacts c ON c.id=l.contact_id '
                          'LEFT JOIN organizations o ON o.id=c.org_id WHERE l.id=?', (lid,))
        if not row:
            raise LookupError('리드를 찾을 수 없습니다.')
        return row

    def campaign(self, cid):
        row = self.db.one('SELECT * FROM campaigns WHERE id=?', (cid,))
        if not row:
            raise LookupError('캠페인을 찾을 수 없습니다.')
        return row

    def add_lead(self, data):
        self.campaign(data['campaign_id'])
        email = email_address(data.get('email', ''))
        name, company = data['name'].strip(), data['company'].strip()
        if not name or not company:
            raise ValueError('이름과 회사명이 필요합니다.')
        domain = norm(data.get('domain', '')).removeprefix('www.').rstrip('/') or None
        if domain and (':' in domain or '/' in domain or ' ' in domain):
            raise ValueError('도메인은 example.com 형식으로 입력하세요.')
        source = public_url(data.get('source_url', ''))
        with self.db.transaction():
            contact = self.db.one('SELECT * FROM contacts WHERE email=?', (email,)) if email else None
            org = self.db.one('SELECT * FROM organizations WHERE domain=?', (domain,)) if domain else None
            if not org:
                # Name matching only suggests an organization; it never collapses different people.
                org = next((r for r in self.db.rows('SELECT * FROM organizations WHERE domain IS NULL') if norm(r['name']) == norm(company)), None) if not domain else None
            if not org:
                oid = self.db.execute('INSERT INTO organizations(name,domain) VALUES(?,?)', (company, domain)).lastrowid
            else:
                oid = org['id']
            possible = self.db.rows('SELECT id,name,email FROM contacts WHERE org_id=?', (oid,))
            # Unknown email identities are held, never automatically merged by name alone.
            ambiguous = not contact and any(norm(c['name']) == norm(name) for c in possible)
            if not contact:
                contact_id = self.db.execute('INSERT INTO contacts(org_id,name,title,email,source_url) VALUES(?,?,?,?,?)',
                                            (oid,name,data.get('title',''),email,source)).lastrowid
            else:
                contact_id = contact['id']
            old = self.db.one('SELECT id FROM leads WHERE campaign_id=? AND contact_id=?', (data['campaign_id'],contact_id))
            if old:
                return {'id':old['id'], 'created':False, 'reason':'동일 캠페인·연락처 중복'}
            stage = 'QA_HOLD' if ambiguous else ('DISCOVERED' if email else 'NO_CONTACT')
            lid = self.db.execute('INSERT INTO leads(campaign_id,contact_id,stage,created_at) VALUES(?,?,?,?)',
                                 (data['campaign_id'],contact_id,stage,now())).lastrowid
            self.db.log('lead_imported', {'identity_review':ambiguous,'source':source}, lid)
            return {'id':lid,'created':True,'identity_review':ambiguous}

    def active(self, lead):
        if lead['suppressed'] or lead['stage'] in ('DISQUALIFIED','LOST','QA_HOLD'):
            raise ValueError('수신거부·거절·부적합·보류 리드는 초안을 만들거나 승인할 수 없습니다.')

    def verify_contact(self, lid, checked, source_url):
        lead = self.lead(lid)
        if not checked or not lead['email'] or not public_url(source_url):
            raise ValueError('업무용 이메일 공개 출처와 수동 확인이 필요합니다.')
        with self.db.transaction():
            self.db.execute('UPDATE contacts SET verified=1,source_url=? WHERE id=?', (source_url,lead['contact_id']))
            self.db.execute("UPDATE leads SET stage='VERIFIED' WHERE id=? AND stage IN ('DISCOVERED','NO_CONTACT','RESEARCH_REQUIRED')", (lid,))
            self.db.log('contact_verified', {'source_url':source_url}, lid)

    def qualify(self,lid,score,fit,rationale):
        lead = self.lead(lid)
        self.active(lead)
        if not lead['verified']:
            raise ValueError('연락처 검증을 먼저 완료하세요.')
        result = qualification(score,fit,rationale,lead['stage'])
        with self.db.transaction():
            self.db.execute('UPDATE leads SET score=?,fit=?,rationale=?,stage=? WHERE id=?',
                            (result['score'],result['fit'],rationale,result['stage'],lid))
            self.db.log('qualified',result,lid)
        return self.lead(lid)

    def suppress(self,lid,reason):
        lead = self.lead(lid)
        with self.db.transaction():
            self.db.execute('UPDATE contacts SET suppressed=1,suppression_reason=? WHERE id=?',(reason,lead['contact_id']))
            self.db.execute("UPDATE tasks SET status='canceled' WHERE lead_id IN (SELECT id FROM leads WHERE contact_id=?)",(lead['contact_id'],))
            self.db.execute("UPDATE drafts SET status='QA_HOLD',approved_version=NULL,approved_at=NULL WHERE lead_id IN (SELECT id FROM leads WHERE contact_id=?)",(lead['contact_id'],))
            self.db.log('suppressed',{'reason':reason},lid)

    def attach(self,lid,filename,content,checked):
        lead = self.lead(lid)
        if len(content)>20*1024*1024 or not content.startswith(b'%PDF-'):
            raise ValueError('20MB 이하 PDF 파일만 등록할 수 있습니다.')
        try:
            pdf=PdfReader(io.BytesIO(content))
            if pdf.is_encrypted or not len(pdf.pages):
                raise ValueError()
        except Exception as exc:
            raise ValueError('유효한 비암호화 PDF가 필요합니다.') from exc
        # Human identity attestation is explicit until the original PPT renderer is connected.
        filename = filename.replace('\\','/').split('/')[-1]
        if '\r' in filename or '\n' in filename or not filename.lower().endswith('.pdf'):
            raise ValueError('PDF 파일명을 확인하세요.')
        with self.db.transaction():
            pid = self.db.execute('INSERT INTO proposals(lead_id,filename,content,sha256,recipient_email,recipient_name,identity_checked,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (lid,filename,content,hashlib.sha256(content).hexdigest(),lead['email'] or '',lead['name'],int(checked),now())).lastrowid
            self.db.log('pdf_uploaded',{'proposal_id':pid,'identity_checked':checked},lid)
        return pid

    def save_draft(self,lid,subject,body,proposal_id=None,step=0):
        lead=self.lead(lid)
        self.active(lead)
        if not lead['email'] or not lead['verified']:
            raise ValueError('검증된 업무용 이메일이 필요합니다.')
        if not subject.strip() or not body.strip():
            raise ValueError('제목과 본문이 필요합니다.')
        existing=self.db.one('SELECT * FROM drafts WHERE lead_id=? AND step=?',(lid,step))
        if existing and existing['sync_state'] in ('SYNCING','UNCERTAIN'):
            raise ValueError('Gmail 동기화 결과를 먼저 확인하세요. 중복 생성 방지를 위해 보류합니다.')
        if proposal_id and not self.db.one('SELECT id FROM proposals WHERE id=? AND lead_id=?',(proposal_id,lid)):
            raise ValueError('다른 고객의 PDF를 첨부할 수 없습니다.')
        thread_id=in_reply_to=''
        if step:
            sent=self.db.rows("SELECT * FROM messages WHERE lead_id=? AND direction='OUT' ORDER BY occurred_at",(lid,))
            if not sent or not sent[-1]['rfc_id']:
                raise ValueError('후속 초안에는 원 발신 메시지와 RFC Message-ID가 필요합니다.')
            first=sent[0]['subject']
            subject=first if first.lower().startswith('re:') else 'Re: '+first
            thread_id,in_reply_to=sent[-1]['thread_id'],sent[-1]['rfc_id']
        with self.db.transaction():
            self.db.execute('INSERT INTO drafts(lead_id,step,subject,body,proposal_id,in_reply_to,thread_id,created_at) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(lead_id,step) DO UPDATE SET subject=excluded.subject,body=excluded.body,proposal_id=excluded.proposal_id, '
                "version=version+1,status='DRAFT_CREATED',approved_version=NULL,approved_at=NULL,approved_by=NULL,sync_state='LOCAL',gmail_hash=NULL",
                (lid,step,subject.strip(),body.strip(),proposal_id,in_reply_to,thread_id,now()))
            draft=self.db.one('SELECT * FROM drafts WHERE lead_id=? AND step=?',(lid,step))
            self.db.log('draft_saved',{'id':draft['id'],'version':draft['version']},lid)
        self.qa(draft['id'])
        return self.draft(draft['id'])

    def draft(self,did):
        d=self.db.one('SELECT * FROM drafts WHERE id=?',(did,))
        if not d: raise LookupError('초안을 찾을 수 없습니다.')
        return d

    def qa(self,did):
        d=self.draft(did); lead=self.lead(d['lead_id']); errors=[]
        if lead['suppressed']: errors.append('수신거부 또는 거절 연락처')
        if not lead['verified'] or not lead['email']: errors.append('연락처 미검증')
        if lead['stage'] in ('QA_HOLD','DISQUALIFIED','LOST'): errors.append('리드 보류 또는 부적합')
        if lead['name'] not in d['body']: errors.append('본문에 수신자 이름이 없습니다.')
        if d['proposal_id']:
            p=self.db.one('SELECT * FROM proposals WHERE id=?',(d['proposal_id'],))
            if not p or p['lead_id']!=lead['id'] or p['recipient_email']!=lead['email'] or not p['identity_checked']:
                errors.append('PDF 수신자 일치 수동 확인 필요')
        elif re.search(r'첨부|attached|attachment',d['body'],re.I): errors.append('첨부 문구가 있으나 PDF 없음')
        # Conservative claim review: every number-bearing sentence must be covered by checked evidence,
        # except fixed program length and meeting CTA. This is a review gate, not an AI truth detector.
        facts=self.db.rows('SELECT claim FROM evidence WHERE contact_id=? AND human_checked=1',(lead['contact_id'],))
        allowed=' '.join(f['claim'] for f in facts)
        text=re.sub(r'https?://\S+','',d['subject']+'\n'+d['body'])
        for sentence in re.split(r'[\n.!?]',text):
            clean=re.sub(r'4\s*주|15\s*[~–-]\s*20\s*분','',sentence)
            if re.search(r'\d',clean) and sentence.strip() not in allowed:
                errors.append('미확인 수치·주장: '+sentence.strip()[:80])
        for c in self.db.rows('SELECT name FROM contacts WHERE id<>?',(lead['contact_id'],)):
            if len(c['name'])>=2 and c['name'] in d['subject']+' '+d['body']:
                errors.append('다른 연락처 이름 포함: '+c['name'])
        if self.db.one("SELECT m.id FROM messages m JOIN leads l ON l.id=m.lead_id WHERE l.contact_id=? AND m.direction='OUT'",(lead['contact_id'],)) and d['step']==0:
            errors.append('이미 연락한 고객: 신규 첫 메일 차단')
        with self.db.transaction():
            self.db.execute('UPDATE drafts SET qa_errors=?,status=? WHERE id=?',
                (json.dumps(errors,ensure_ascii=False),'QA_HOLD' if errors else ('APPROVED' if d['approved_version']==d['version'] else 'QA_PASS'),did))
            if errors:
                self.db.execute('UPDATE drafts SET approved_version=NULL,approved_at=NULL WHERE id=?',(did,))
        return errors

    def approve(self,did,version,actor,confirmed):
        with self.db.transaction():
            d=self.draft(did)
            if not confirmed or not actor.strip() or version!=d['version']:
                raise ValueError('현재 버전에 대한 담당자 명시적 승인이 필요합니다.')
            if self.qa(did): raise ValueError('QA 오류를 먼저 해결하세요.')
            if d['sync_state']!='VERIFIED': raise ValueError('Gmail 초안과 첨부를 먼저 생성·재검증하세요.')
            self.db.execute("UPDATE drafts SET status='APPROVED',approved_at=?,approved_by=?,approved_version=version WHERE id=?",(now(),actor,did))
            self.db.log('draft_approved',{'draft_id':did,'version':version,'actor':actor},d['lead_id'])
        return self.draft(did)

    def sync_draft(self,did):
        # Hold the per-process write lock across Gmail IO. Never retry an ambiguous create automatically.
        with self.db.lock:
            if self.qa(did): raise ValueError('QA 오류를 먼저 해결하세요.')
            d=self.draft(did); lead=self.lead(d['lead_id']); campaign=self.campaign(lead['campaign_id'])
            if d['sync_state'] in ('SYNCING','UNCERTAIN'):
                raise ValueError('이전 Gmail 요청의 결과가 불명확합니다. Gmail에서 확인 후 수동 복구가 필요합니다.')
            client=self.gmail_factory()
            if email_address(client.account())!=email_address(campaign['sender']): raise ValueError('OAuth 발신 계정이 캠페인과 다릅니다.')
            pdf=self.db.one('SELECT * FROM proposals WHERE id=?',(d['proposal_id'],)) if d['proposal_id'] else None
            raw=mime_message(campaign['sender'],lead['email'],d['subject'],d['body'],pdf,d['in_reply_to'])
            with self.db.transaction():
                self.db.execute("UPDATE drafts SET sync_state='SYNCING',approved_version=NULL,approved_at=NULL,status='QA_PASS' WHERE id=?",(did,))
            try:
                result=client.upsert(d['gmail_draft_id'],raw,d['thread_id'])
                gid=result['id']
                with self.db.transaction():
                    self.db.execute('UPDATE drafts SET gmail_draft_id=? WHERE id=?',(gid,did))
                verified=client.read(gid)
                verify_raw(verified,campaign['sender'],lead['email'],d['subject'],d['body'],pdf)
                with self.db.transaction():
                    self.db.execute("UPDATE drafts SET sync_state='VERIFIED',gmail_hash=? WHERE id=?",(hashlib.sha256(verified.encode()).hexdigest(),did))
                    self.db.log('gmail_draft_verified',{'draft_id':did,'gmail_id':gid},lead['id'])
            except Exception:
                with self.db.transaction():
                    self.db.execute("UPDATE drafts SET sync_state='UNCERTAIN',status='QA_HOLD' WHERE id=?",(did,))
                    self.db.execute("INSERT INTO sync_runs(source,status,detail,at) VALUES('gmail_draft','FAILED','Gmail 결과 확인 또는 첨부 재검증 필요',?)",(now(),))
                raise ValueError('Gmail 초안 생성·재검증 실패. 중복 생성 방지를 위해 보류했습니다.')
        return self.draft(did)

    def import_message(self,account,item):
        # Exact thread ownership plus envelope validation, then RFC fallback. Never match by subject/name.
        account=email_address(account); sender=email_address(parseaddr(item['from'])[1]); recipient=email_address(parseaddr(item['to'])[1])
        direction='OUT' if sender==account else 'IN'
        if direction=='IN' and recipient!=account: raise ValueError('수신 계정 불일치')
        other=recipient if direction=='OUT' else sender
        rows=self.db.rows('SELECT DISTINCT l.id FROM messages m JOIN leads l ON l.id=m.lead_id JOIN contacts c ON c.id=l.contact_id '
            'WHERE m.account=? AND m.thread_id=? AND c.email=?',(account,item['thread_id'],other))
        if not rows and item.get('in_reply_to'):
            rows=self.db.rows('SELECT DISTINCT l.id FROM messages m JOIN leads l ON l.id=m.lead_id JOIN contacts c ON c.id=l.contact_id '
                'WHERE m.account=? AND m.rfc_id=? AND c.email=?',(account,item['in_reply_to'],other))
        if not rows and direction=='OUT' and item.get('lead_id'):
            lead=self.lead(item['lead_id']); campaign=self.campaign(lead['campaign_id'])
            if email_address(campaign['sender'])!=account or lead['email']!=other: raise ValueError('발신 메시지와 리드 불일치')
            rows=[{'id':lead['id']}]
        if len(rows)!=1: raise ValueError('스레드 매칭 불명확: 담당자 검토 필요')
        lid=rows[0]['id']; intent=item.get('intent','UNCLASSIFIED')
        with self.db.transaction():
            existing=self.db.one('SELECT id FROM messages WHERE account=? AND gmail_id=?',(account,item['gmail_id']))
            if existing: return {'created':False,'lead_id':lid}
            self.db.execute('INSERT INTO messages(account,gmail_id,thread_id,rfc_id,lead_id,direction,intent,subject,snippet,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (account,item['gmail_id'],item['thread_id'],item.get('rfc_id',''),lid,direction,intent,item.get('subject',''),item.get('snippet',''),item['occurred_at']))
            if direction=='OUT':
                self.db.execute("UPDATE leads SET stage='CONTACTED' WHERE id=? AND stage IN ('DISCOVERED','VERIFIED','QUALIFIED','PERSONALIZED','READY_FOR_APPROVAL')",(lid,))
                step=self.db.one('SELECT count(*) n FROM messages WHERE lead_id=? AND direction=\'OUT\'',(lid,))['n']-1
                schedule_next_touch(self.db,self.lead(lid),step)
            elif intent not in ('AUTO_REPLY','BOUNCE'):
                self.db.execute("UPDATE tasks SET status='canceled' WHERE lead_id=? AND kind='followup_email'",(lid,))
                self.db.execute("UPDATE leads SET stage='IN_CONVERSATION' WHERE id=? AND stage IN ('CONTACTED','FOLLOWUP_DUE','NO_REPLY')",(lid,))
            self.db.log('message_imported',{'gmail_id':item['gmail_id'],'direction':direction,'intent':intent},lid)
        if intent in ('OPT_OUT','REJECTED'): self.suppress(lid,intent)
        return {'created':True,'lead_id':lid}

    def complete_meeting(self,mid,notes,actor,confirmed,next_step):
        meeting=self.db.one('SELECT * FROM meetings WHERE id=?',(mid,))
        if not meeting: raise LookupError('미팅이 없습니다.')
        if not confirmed or not notes.strip() or not actor.strip(): raise ValueError('실제 완료 확인·회의록·담당자가 필요합니다.')
        if meeting['status']!='BOOKED': raise ValueError('예약된 미팅만 완료 처리할 수 있습니다.')
        if datetime.fromisoformat(meeting['starts_at'])>datetime.now(timezone.utc): raise ValueError('미래 미팅은 완료할 수 없습니다.')
        with self.db.transaction():
            self.db.execute("UPDATE meetings SET status='COMPLETED',completed_at=?,confirmed_by=?,notes=?,next_step=? WHERE id=?",(now(),actor,notes,next_step,mid))
            self.db.execute("UPDATE leads SET stage='MEETING_COMPLETED' WHERE id=? AND stage NOT IN ('WON','LOST')",(meeting['lead_id'],))
            self.db.log('meeting_completed',{'meeting_id':mid,'actor':actor,'notes':notes},meeting['lead_id'])

    def funnel(self):
        scalar=lambda sql: self.db.one(sql)['n']
        return dict(scope='현재 로컬 DB 전체 / 고유 Contact ID / 전체 기간',date_basis='활동 발생일 · 기간 필터 미적용',
            contacts=scalar('SELECT count(*) n FROM contacts'),leads=scalar('SELECT count(*) n FROM leads'),
            contacted=scalar("SELECT count(DISTINCT l.contact_id) n FROM messages m JOIN leads l ON l.id=m.lead_id WHERE direction='OUT'"),
            valid_email=scalar("SELECT count(DISTINCT l.contact_id) n FROM messages m JOIN leads l ON l.id=m.lead_id WHERE direction='OUT' AND NOT EXISTS(SELECT 1 FROM messages b JOIN leads bl ON bl.id=b.lead_id WHERE bl.contact_id=l.contact_id AND b.intent='BOUNCE')"),
            replied=scalar("SELECT count(DISTINCT l.contact_id) n FROM messages m JOIN leads l ON l.id=m.lead_id WHERE direction='IN' AND intent IN ('HUMAN','POSITIVE','QUESTION','REJECTED','OPT_OUT')"),
            positive=scalar("SELECT count(DISTINCT l.contact_id) n FROM messages m JOIN leads l ON l.id=m.lead_id WHERE direction='IN' AND intent='POSITIVE'"),
            booked=scalar("SELECT count(DISTINCT l.contact_id) n FROM meetings m JOIN leads l ON l.id=m.lead_id WHERE m.status IN ('BOOKED','COMPLETED')"),
            completed=scalar("SELECT count(DISTINCT l.contact_id) n FROM meetings m JOIN leads l ON l.id=m.lead_id WHERE m.status='COMPLETED'"),
            contracted=scalar("SELECT count(DISTINCT l.contact_id) n FROM deals d JOIN leads l ON l.id=d.lead_id WHERE d.stage IN ('CONTRACTED','PAID')"),
            revenue=scalar("SELECT coalesce(sum(amount_krw),0) n FROM deals WHERE stage='PAID'"),
            drafts=scalar('SELECT count(*) n FROM drafts'),approved=scalar("SELECT count(*) n FROM drafts WHERE status='APPROVED'"),
            qa_hold=scalar("SELECT count(*) n FROM drafts WHERE status='QA_HOLD'"),send_enabled=False)
