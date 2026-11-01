"""Read-only captured Google Sheet rows. Stable provider IDs, no synthetic Gmail thread IDs."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from .db import now

SHEET_ID='1u9r1phJu6tn-4RyVgJnZIuwMf_Ru8qX5WSCEcyewUcE'
SOURCE='https://docs.google.com/spreadsheets/d/'+SHEET_ID+'/edit'


def import_snapshot(wf,snapshot,campaign_id):
    db=wf.db; result={'contacts_created':0,'proposals_created':0,'sent_events_created':0,'source_modified':False}
    pipeline=snapshot['pipeline']; history=snapshot.get('history',[])
    h={r['리드ID']:r for r in history if r.get('원천')=='GMAIL_SENT'}
    for row in pipeline:
        external=row['리드ID']; before=db.one('SELECT id FROM contacts WHERE email=?',(row['이메일'].strip().lower(),))
        added=wf.add_lead({'campaign_id':campaign_id,'name':row['받는 사람'],'company':row['회사/브랜드'],
                         'email':row['이메일'],'source_url':SOURCE})
        lid=added['id']; result['contacts_created']+=int(not before)
        with db.transaction():
            lead=wf.lead(lid)
            db.execute('UPDATE organizations SET industry=? WHERE id=(SELECT org_id FROM contacts WHERE id=?)',(row.get('업종',''),lead['contact_id']))
            db.execute('INSERT INTO source_records(provider,external_id,lead_id,payload,imported_at) VALUES(?,?,?,?,?) '
                'ON CONFLICT(provider,external_id) DO NOTHING',('google_sheet',SHEET_ID+':'+external,lid,json.dumps(row,ensure_ascii=False),now()))
            url=row.get('PDF 링크')
            if url and not db.one('SELECT id FROM proposals WHERE lead_id=? AND source_url=?',(lid,url)):
                db.execute('INSERT INTO proposals(lead_id,filename,content,sha256,recipient_email,recipient_name,created_at,qa_status,source_url) VALUES(?,?,?,?,?,?,?,?,?)',
                    (lid,'historical-reference.pdf',b'','',lead['email'],lead['name'],now(),'SOURCE_ONLY',url))
                result['proposals_created']+=1
            event=h.get(external)
            if event:
                gid=event['이벤트 키'].removeprefix('mail:')
                old=db.one('SELECT id FROM messages WHERE account=? AND gmail_id=?',('marketing@creatobi.com',gid))
                if not old:
                    occurred=datetime.strptime(event['원천 발생일시'],'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Seoul')).isoformat()
                    db.execute('INSERT INTO messages(account,gmail_id,thread_id,lead_id,direction,subject,occurred_at,source) VALUES(?,?,?,?,?,?,?,?)',
                        ('marketing@creatobi.com',gid,'',lid,'OUT',event['제목·일정명'],occurred,'google_sheet_history'))
                    db.execute("UPDATE leads SET stage='CONTACTED' WHERE id=? AND stage='DISCOVERED'",(lid,))
                    result['sent_events_created']+=1
            # Meeting/contract/revenue unknowns stay unknown; imported cells never mutate manual deals.
    with db.transaction():
        db.log('sheet_readonly_import',result)
        db.execute('INSERT OR REPLACE INTO integration_health(provider,state,detail,checked_at) VALUES(?,?,?,?)',
            ('sheets','REAL','연결된 Google Drive에서 읽은 원본 스냅샷 가져오기 · 원본 쓰기 호출 없음',now()))
    return result
