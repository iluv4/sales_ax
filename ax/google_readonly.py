import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from .gmail import GmailDraftClient
from .db import now


def calendar_service():
    path=os.getenv('CALENDAR_AUTHORIZED_USER_FILE') or os.getenv('GMAIL_AUTHORIZED_USER_FILE')
    if not path:raise ValueError('Calendar OAuth 자격증명이 없습니다.')
    creds=Credentials.from_authorized_user_file(path,scopes=['https://www.googleapis.com/auth/calendar.readonly'])
    return build('calendar','v3',credentials=creds,cache_discovery=False)


def sheets_service():
    path=os.getenv('SHEETS_AUTHORIZED_USER_FILE')
    if not path:raise ValueError('Sheets 읽기 전용 OAuth 자격증명이 없습니다.')
    creds=Credentials.from_authorized_user_file(path,scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    return build('sheets','v4',credentials=creds,cache_discovery=False)


def sync_calendar(wf,service=None):
    service=service or calendar_service();db=wf.db;created=0;unmatched=0;token=None
    while True:
        response=service.events().list(calendarId='primary',singleEvents=True,maxResults=250,pageToken=token).execute()
        for event in response.get('items',[]):
            external=event['id'];old=db.one('SELECT * FROM meetings WHERE external_id=?',(external,))
            if old:
                # Calendar never overwrites human-confirmed completion or manual deal/revenue.
                if old['status']!='COMPLETED' and event.get('status')=='cancelled':
                    with db.transaction():db.execute("UPDATE meetings SET status='CANCELLED' WHERE id=?",(old['id'],))
                continue
            attendees={a.get('email','').lower() for a in event.get('attendees',[])}
            matches=db.rows('SELECT l.id,c.email FROM leads l JOIN contacts c ON c.id=l.contact_id')
            matches=[l for l in matches if l['email'] in attendees]
            start=event.get('start',{}).get('dateTime')
            if len(matches)!=1 or not start or event.get('status')=='cancelled':unmatched+=1;continue
            with db.transaction():
                db.execute('INSERT INTO meetings(lead_id,external_id,starts_at) VALUES(?,?,?)',(matches[0]['id'],external,start))
                db.log('calendar_booking_imported',{'event_id':external},matches[0]['id'])
            created+=1
        token=response.get('nextPageToken')
        if not token:break
    with db.transaction():
        db.execute('INSERT INTO sync_runs(source,status,detail,at) VALUES(?,?,?,?)',('calendar','SUCCESS',f'created={created}, unmatched={unmatched}',now()))
    return {'created':created,'unmatched':unmatched,'read_only':True}
