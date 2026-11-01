"""No FeatPaper HTTP API assumptions. Evidence-driven manual and mapped CSV tracking."""
import csv
import hashlib
import io
import json
import secrets
from datetime import datetime
from typing import Protocol
from .db import now
from .service import public_url


class DocumentTrackingProvider(Protocol):
    def attach_link(self,proposal_id:int,lead_id:int,url:str,external_doc_id:str|None=None): ...
    def import_events(self,payload): ...
    def availability(self): ...


class FeatPaperManualProvider:
    provider='featpaper_manual'
    def __init__(self,db):self.db=db
    def availability(self):
        return {'state':'MANUAL','attach_link':True,'upload':False,'automatic_analytics':False,'view_without_evidence':'unknown'}
    def attach_link(self,proposal_id,lead_id,url,external_doc_id=None):
        if not public_url(url): raise ValueError('수신자별 실제 링크가 필요합니다.')
        p=self.db.one('SELECT id FROM proposals WHERE id=? AND lead_id=?',(proposal_id,lead_id))
        if not p: raise ValueError('제안서와 고객 매핑이 일치하지 않습니다.')
        with self.db.transaction():
            old=self.db.one('SELECT * FROM document_links WHERE provider=? AND url=?',(self.provider,url))
            if old:
                if old['lead_id']!=lead_id or old['proposal_id']!=proposal_id: raise ValueError('다른 고객에게 같은 링크를 재사용할 수 없습니다.')
                return old
            lid=self.db.execute('INSERT INTO document_links(proposal_id,lead_id,provider,url,external_doc_id,created_at) VALUES(?,?,?,?,?,?)',
                (proposal_id,lead_id,self.provider,url,external_doc_id,now())).lastrowid
            self.db.log('document_link_attached',{'link_id':lid,'provider':self.provider},lead_id)
        return self.db.one('SELECT * FROM document_links WHERE id=?',(lid,))
    def import_events(self,payload):
        if not payload.get('confirmed') or not payload.get('observation_source'):
            raise ValueError('실제 열람 확인 근거와 담당자 확인이 필요합니다.')
        public_url(payload['observation_source'])
        return record_event(self.db,self.provider,payload,'manual_import')


def record_event(db,provider,payload,source):
    link=db.one('SELECT * FROM document_links WHERE id=?',(payload['link_id'],))
    if not link: raise LookupError('문서 링크가 없습니다.')
    if source!='internal_viewer' and link['provider']!=provider and provider!='featpaper_csv':
        raise ValueError('문서 추적 제공자 불일치')
    if payload['event_type'] not in ('document_open','page_visible','dwell','view_observed'):
        raise ValueError('지원되지 않는 문서 이벤트')
    stamp=datetime.fromisoformat(payload['occurred_at'])
    if stamp.tzinfo is None: raise ValueError('이벤트 시간대가 필요합니다.')
    if payload.get('page_number') is not None and payload['page_number']<1: raise ValueError('페이지는 1부터 시작합니다.')
    dwell=payload.get('dwell_ms')
    if dwell is not None and not 0<=dwell<=60000: raise ValueError('체류 이벤트는 0~60초 단위여야 합니다.')
    with db.transaction():
        existing=db.one('SELECT id FROM document_events WHERE provider=? AND external_event_id=?',(provider,payload.get('external_event_id')))
        if existing:return {'created':False,'id':existing['id']}
        eid=db.execute('INSERT INTO document_events(link_id,provider,event_type,page_number,dwell_ms,occurred_at,source,external_event_id,is_mock,observation_source,session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (link['id'],provider,payload['event_type'],payload.get('page_number'),dwell,payload['occurred_at'],source,payload.get('external_event_id'),int(payload.get('is_mock',False)),payload.get('observation_source'),payload.get('session_id'))).lastrowid
        if not payload.get('is_mock',False):
            db.execute("UPDATE document_links SET verification_state='observed' WHERE id=?",(link['id'],))
    return {'created':True,'id':eid}


class FeatPaperCsvProvider:
    provider='featpaper_csv'
    def __init__(self,db):self.db=db
    def availability(self): return {'state':'BLOCKED','reason':'실제 Pro CSV 샘플·열 매핑 승인 전에는 형식을 추정하지 않습니다.'}
    def import_events(self,payload):
        mapping=payload['mapping']; text=payload['csv']; confirmed=payload.get('confirmed',False)
        rows=list(csv.DictReader(io.StringIO(text)))
        headers=list(rows[0]) if rows else []
        if not all(k in mapping for k in ('link_id','event_type','occurred_at')): raise ValueError('링크 ID·이벤트 종류·발생일시 매핑이 필요합니다.')
        if any(v not in headers for v in mapping.values()): raise ValueError('실제 CSV에 없는 열은 매핑할 수 없습니다.')
        parsed=[]
        allowed={'link_id','event_type','occurred_at','page_number','dwell_ms','external_event_id','is_mock'}
        if set(mapping)-allowed: raise ValueError('지원하지 않는 CSV 매핑')
        for row in rows:
            x={k:row[v] for k,v in mapping.items() if row[v]!=''}
            for key in ('link_id','page_number','dwell_ms'):
                if key in x:x[key]=int(x[key])
            x['is_mock']=str(x.get('is_mock','false')).lower()=='true'
            # Stable digest only deduplicates this exact source row, never guesses identity.
            x.setdefault('external_event_id',hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest())
            parsed.append(x)
        if not confirmed:return {'preview':parsed[:20],'rows':len(parsed),'requires_confirmation':True}
        count=0
        for x in parsed:count+=int(record_event(self.db,self.provider,x,'featpaper_csv')['created'])
        return {'created':count,'duplicates':len(parsed)-count}


class InternalTrackedPDFProvider(FeatPaperManualProvider):
    provider='internal_pdf'
    def attach_link(self,proposal_id,lead_id,url='',external_doc_id=None):
        token=secrets.token_urlsafe(32)
        link=super().attach_link(proposal_id,lead_id,'http://127.0.0.1:8765/p/'+token)
        with self.db.transaction(): self.db.execute('UPDATE document_links SET token_hash=? WHERE id=?',(hashlib.sha256(token.encode()).hexdigest(),link['id']))
        return {**link,'token':token,'label':'내부 문서 뷰어','warning':'로컬 주소입니다. 외부 수신자는 접근할 수 없습니다.'}
