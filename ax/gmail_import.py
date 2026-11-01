"""Pull the operator's real outreach out of Gmail: every 1:1 sent message becomes a lead plus its thread replies.

The sheet snapshot only knows a few leads, but most outreach went to people who were never in the sheet.
Read-only: lists SENT, reads threads, never sends or modifies mail.
"""
import json
from datetime import datetime,timezone
from email.utils import getaddresses,parseaddr
from .db import now

AUTO_CAMPAIGN='Gmail 보낸 메일 (자동 가져오기)'
FREE_MAIL={'gmail.com','naver.com','daum.net','hanmail.net','kakao.com','nate.com','hotmail.com','outlook.com','icloud.com','yahoo.com'}


def headers(msg):return {h['name'].lower():h['value'] for h in msg.get('payload',{}).get('headers',[])}


def occurred(msg):return datetime.fromtimestamp(int(msg['internalDate'])/1000,timezone.utc).isoformat()


def campaign_id(db,account):
    row=db.one('SELECT id FROM campaigns WHERE name=?',(AUTO_CAMPAIGN,))
    if row:return row['id']
    icp=json.dumps({k:[] for k in ('industries','regions','buyer_titles','pain_points','disqualifiers')}|{'company_size':''},ensure_ascii=False)
    with db.transaction():
        return db.execute('INSERT INTO campaigns(name,motion,sender,icp,product,value_prop,booking_link,target_daily,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (AUTO_CAMPAIGN,'EXPERT_PARTNER',account,icp,'모범AI 강사 섭외','Gmail 보낸편지함에서 가져온 실제 발송','',200,now())).lastrowid


def lead_for(wf,cid,name,email):
    db=wf.db
    existing=db.rows('SELECT l.id FROM leads l JOIN contacts c ON c.id=l.contact_id WHERE c.email=?',(email,))
    if len(existing)==1:return existing[0]['id']
    if existing:return None  # same person in several campaigns: leave for manual matching
    domain=email.split('@')[1]
    company=(name or email.split('@')[0]) if domain in FREE_MAIL else domain
    return wf.add_lead({'campaign_id':cid,'name':name or email.split('@')[0],'company':company,'email':email,
                        'source_url':'https://mail.google.com/'})['id']


def import_sent(wf,client,days=60,limit=500):
    db=wf.db;account=client.account();own=account.split('@')[1];cid=campaign_id(db,account)
    result={'sent_seen':0,'sent_imported':0,'replies_imported':0,'leads_created':0,'skipped':0,'days':days}
    threads=set();token=None;service=client.service.users().messages()
    before=db.one('SELECT count(*) n FROM leads')['n']
    while result['sent_seen']<limit:
        page=service.list(userId='me',q=f'in:sent newer_than:{days}d',maxResults=min(100,limit-result['sent_seen']),pageToken=token).execute()
        for item in page.get('messages',[]):
            result['sent_seen']+=1
            if db.one('SELECT id FROM messages WHERE account=? AND gmail_id=?',(account,item['id'])):threads.add(item['threadId']);continue
            msg=service.get(userId='me',id=item['id'],format='metadata',metadataHeaders=['To','Cc','Bcc','From','Subject','Message-ID']).execute()
            h=headers(msg);to=[a for _,a in getaddresses([h.get('to','')]) if a]
            # Only unambiguous 1:1 outreach to someone outside the company becomes a lead.
            if len(to)!=1 or h.get('cc') or h.get('bcc') or to[0].lower().endswith('@'+own):result['skipped']+=1;continue
            email=to[0].lower();name=parseaddr(h.get('to',''))[0].strip().strip('"')
            try:
                lid=lead_for(wf,cid,name,email)
                if not lid:result['skipped']+=1;continue
                made=wf.import_message(account,{'gmail_id':msg['id'],'thread_id':msg['threadId'],'from':h.get('from',account),'to':email,
                    'rfc_id':h.get('message-id',''),'subject':h.get('subject',''),'snippet':msg.get('snippet',''),'occurred_at':occurred(msg),'lead_id':lid})
                result['sent_imported']+=int(made['created']);threads.add(msg['threadId'])
            except ValueError:result['skipped']+=1
        token=page.get('nextPageToken')
        if not token:break
    for thread in threads:
        for msg in client.thread_messages(thread):
            if 'DRAFT' in msg.get('labelIds',[]) or 'SENT' in msg.get('labelIds',[]):continue
            h=headers(msg);sender=h.get('from','').lower()
            intent='AUTO_REPLY' if h.get('auto-submitted','no')!='no' else ('BOUNCE' if 'mailer-daemon' in sender or 'postmaster' in sender else 'UNCLASSIFIED')
            try:
                made=wf.import_message(account,{'gmail_id':msg['id'],'thread_id':msg['threadId'],'from':h.get('from',''),'to':account,
                    'rfc_id':h.get('message-id',''),'in_reply_to':h.get('in-reply-to',''),'subject':h.get('subject',''),
                    'snippet':msg.get('snippet',''),'occurred_at':occurred(msg),'intent':intent})
                result['replies_imported']+=int(made['created'])
            except ValueError:pass
    result['leads_created']=db.one('SELECT count(*) n FROM leads')['n']-before
    with db.transaction():
        db.execute('INSERT INTO sync_runs(source,status,detail,at) VALUES(?,?,?,?)',('gmail_sent_import','SUCCESS',json.dumps(result,ensure_ascii=False),now()))
    return result
