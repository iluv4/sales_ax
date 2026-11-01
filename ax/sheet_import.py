"""Read-only captured Google Sheet rows. Stable provider IDs, no synthetic Gmail thread IDs.

The original sheet is never written. Rows arrive as an exported CSV (or JSON snapshot) and are keyed by
SHEET_ID:리드ID so a second import links to the same lead instead of creating people again.
"""
import csv
import io
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from .db import now

SHEET_ID='1u9r1phJu6tn-4RyVgJnZIuwMf_Ru8qX5WSCEcyewUcE'
SOURCE='https://docs.google.com/spreadsheets/d/'+SHEET_ID+'/edit'
ACCOUNT='marketing@creatobi.com'
PIPELINE_COLUMNS=('리드ID','받는 사람','회사/브랜드','이메일')
HISTORY_COLUMNS=('리드ID','원천','이벤트 키','원천 발생일시')


def read_csv(text,required):
    """Exported tabs can carry title rows above the header; use the first row that names every required column."""
    rows=list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    for i,row in enumerate(rows):
        header=[c.strip() for c in row]
        if all(c in header for c in required):
            return [dict(zip(header,[v.strip() for v in r])) for r in rows[i+1:] if any(v.strip() for v in r)]
    raise ValueError('필수 열이 없습니다: '+', '.join(required))


def sent_at(text):
    for fmt in ('%Y-%m-%d %H:%M','%Y-%m-%d %H:%M:%S','%Y. %m. %d %H:%M','%Y/%m/%d %H:%M'):
        try:return datetime.strptime(text,fmt).replace(tzinfo=ZoneInfo('Asia/Seoul')).isoformat()
        except ValueError:pass
    raise ValueError('발송 시각 형식을 읽을 수 없습니다: '+text)


def plan(wf,snapshot):
    """Dry run: what an import would do. Writes nothing."""
    db=wf.db;items=[]
    for row in snapshot['pipeline']:
        external=row.get('리드ID','')
        if not external:items.append({'row':row,'action':'ERROR','reason':'리드ID 없음'});continue
        old=db.one("SELECT lead_id FROM source_records WHERE provider='google_sheet' AND external_id=?",(SHEET_ID+':'+external,))
        email=row.get('이메일','').strip().lower()
        contact=db.one('SELECT id FROM contacts WHERE email=?',(email,)) if email else None
        items.append({'external_id':external,'name':row.get('받는 사람',''),'action':'EXISTING' if old else ('LINK_CONTACT' if contact else 'NEW'),
                      'sent_events':sum(1 for h in snapshot.get('history',[]) if h.get('리드ID')==external and h.get('원천')=='GMAIL_SENT')})
    return {'rows':len(items),'new':sum(i['action']=='NEW' for i in items),'existing':sum(i['action']=='EXISTING' for i in items),
            'link_contact':sum(i['action']=='LINK_CONTACT' for i in items),'errors':sum(i['action']=='ERROR' for i in items),
            'items':[{k:v for k,v in i.items() if k!='row'} for i in items],'source_modified':False,'requires_confirmation':True}


def import_snapshot(wf,snapshot,campaign_id):
    db=wf.db;wf.campaign(campaign_id)
    result={'contacts_created':0,'leads_created':0,'existing':0,'proposals_created':0,'sent_events_created':0,'errors':[],'source_modified':False}
    history={}
    for h in snapshot.get('history',[]):
        if h.get('원천')=='GMAIL_SENT':history.setdefault(h.get('리드ID'),[]).append(h)
    for row in snapshot['pipeline']:
        external=row.get('리드ID','')
        try:
            if not external:raise ValueError('리드ID 없음')
            key=SHEET_ID+':'+external
            known=db.one("SELECT lead_id FROM source_records WHERE provider='google_sheet' AND external_id=?",(key,))
            if known:
                lid=known['lead_id'];result['existing']+=1
            else:
                email=row.get('이메일','').strip().lower()
                before=db.one('SELECT id FROM contacts WHERE email=?',(email,)) if email else None
                added=wf.add_lead({'campaign_id':campaign_id,'name':row.get('받는 사람',''),'company':row.get('회사/브랜드',''),'email':email,'source_url':SOURCE})
                lid=added['id'];result['contacts_created']+=int(not before);result['leads_created']+=int(added['created'])
                with db.transaction():
                    db.execute('INSERT INTO source_records(provider,external_id,lead_id,payload,imported_at) VALUES(?,?,?,?,?)',
                        ('google_sheet',key,lid,json.dumps(row,ensure_ascii=False),now()))
            lead=wf.lead(lid)
            with db.transaction():
                if row.get('업종'):db.execute('UPDATE organizations SET industry=? WHERE id=(SELECT org_id FROM contacts WHERE id=?)',(row['업종'],lead['contact_id']))
                url=row.get('PDF 링크')
                if url and not db.one('SELECT id FROM proposals WHERE lead_id=? AND source_url=?',(lid,url)):
                    # Reference only: the historical file is not copied, so it can never be attached to a new draft.
                    db.execute('INSERT INTO proposals(lead_id,filename,content,sha256,recipient_email,recipient_name,created_at,qa_status,source_url) VALUES(?,?,?,?,?,?,?,?,?)',
                        (lid,'historical-reference.pdf',b'','',lead['email'] or '',lead['name'],now(),'SOURCE_ONLY',url))
                    result['proposals_created']+=1
            for event in history.get(external,[]):
                key=event.get('이벤트 키','')
                if not key.startswith('mail:'):result['errors'].append({'external_id':external,'reason':'Gmail 메시지 키가 아닌 발송 이벤트'});continue
                gid=key.removeprefix('mail:')
                if db.one('SELECT id FROM messages WHERE account=? AND gmail_id=?',(ACCOUNT,gid)):continue
                occurred=sent_at(event.get('원천 발생일시',''))
                with db.transaction():
                    db.execute('INSERT INTO messages(account,gmail_id,thread_id,lead_id,direction,subject,occurred_at,source) VALUES(?,?,?,?,?,?,?,?)',
                        (ACCOUNT,gid,'',lid,'OUT',event.get('제목·일정명',''),occurred,'google_sheet_history'))
                    db.execute("UPDATE leads SET stage='CONTACTED' WHERE id=? AND stage IN ('DISCOVERED','NO_CONTACT')",(lid,))
                    db.log('sheet_sent_event_imported',{'gmail_id':gid,'thread_id_known':False},lid)
                result['sent_events_created']+=1
            # Meeting/contract/revenue cells stay unknown; imported cells never mutate manual deals.
        except (ValueError,LookupError) as exc:result['errors'].append({'external_id':external,'reason':str(exc)})
    with db.transaction():
        db.log('sheet_readonly_import',{k:v for k,v in result.items() if k!='errors'}|{'errors':len(result['errors'])})
        db.execute('INSERT INTO sync_runs(source,status,detail,at) VALUES(?,?,?,?)',('google_sheet','PARTIAL' if result['errors'] else 'SUCCESS',
            json.dumps({k:v for k,v in result.items() if k!='errors'}|{'errors':len(result['errors'])},ensure_ascii=False),now()))
        db.execute('INSERT OR REPLACE INTO integration_health(provider,state,detail,checked_at) VALUES(?,?,?,?)',
            ('sheets','MANUAL','내보낸 시트 파일 읽기 전용 가져오기 · Google API 직접 연결 아님 · 원본 쓰기 없음',now()))
    return result
