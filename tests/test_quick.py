import io
from fastapi.testclient import TestClient
from pypdf import PdfWriter
import pytest
from ax.api import create_app
from ax.gmail import decode_raw


class FakeWriter:
    def __init__(self):self.calls=[]
    def __call__(self,prompt,schema,name,web=False):
        self.calls.append((name,web,prompt))
        if name=='candidates':return {'candidates':[
            {'name':'김가상','instagram':'@fake_tax','followers':3200,'email':'tax@example.com','website':'https://example.com','category':'세무',
             'evidence':'쇼핑몰 세무 콘텐츠를 매주 올림','source_url':'https://example.com/post'},
            {'name':'이큰손','instagram':'big_one','followers':52000,'email':'big@example.com','website':'','category':'세무','evidence':'','source_url':''},
            {'name':'박무메일','instagram':'no_mail','followers':900,'email':'','website':'','category':'학원','evidence':'학원 운영 브이로그','source_url':''}]}
        return {'subject':'세무 콘텐츠 보고 연락드립니다','body':'김가상님 안녕하세요\n본문'}


class FakeGmail:
    def __init__(self):self.drafts=[]
    def account(self):return 'marketing@creatobi.com'
    def upsert(self,draft_id,raw,thread_id=''):self.drafts.append((draft_id,raw));return {'id':draft_id or 'draft-1'}


@pytest.fixture
def quick(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.delenv('SALES_AX_PUBLIC_HOST',raising=False)
    gmail=FakeGmail();writer=FakeWriter()
    app=create_app(tmp_path/'q.db',gmail_factory=lambda:gmail)
    app.state.quick_writer=writer
    with TestClient(app) as c:yield c,writer,gmail


def pdf_bytes():
    out=io.BytesIO();w=PdfWriter();w.add_blank_page(width=200,height=200);w.write(out);return out.getvalue()


def test_search_filters_followers_dedupes_and_persists(quick):
    c,writer,_=quick
    assert c.get('/quick').status_code==200
    r=c.post('/api/quick/search',json={'query':'인스타 세무사','count':5,'max_followers':10000}).json()
    assert [a['name'] for a in r['added']]==['김가상','박무메일'] and r['added'][0]['instagram']=='fake_tax'
    assert writer.calls[0][:2]==('candidates',True) and '10,000명 이하' in writer.calls[0][2]
    again=c.post('/api/quick/search',json={'query':'인스타 세무사'}).json()
    assert again['added']==[] and again['total']==2
    assert len(c.get('/api/quick/state').json()['rows'])==2
    assert '김가상' in c.get('/api/quick/rows.csv').text


def test_draft_uses_brochure_and_gmail_attaches_pdf_without_sending(quick):
    c,writer,gmail=quick
    c.post('/api/quick/search',json={'query':'세무사'})
    rows=c.get('/api/quick/state').json()['rows'];rid=rows[0]['id'];nomail=rows[1]['id']
    assert c.post('/api/quick/brochure',files={'file':('소개서.pdf',b'not a pdf','application/pdf')}).status_code==400
    assert c.post('/api/quick/brochure',files={'file':('소개서.pdf',pdf_bytes(),'application/pdf')}).json()['filename']=='소개서.pdf'
    assert c.post(f'/api/quick/rows/{rid}/gmail').status_code==400
    drafted=c.post(f'/api/quick/rows/{rid}/draft').json()
    assert drafted['status']=='메일 작성됨' and '쇼핑몰 세무 콘텐츠' in writer.calls[-1][2] and writer.calls[-1][1] is False
    made=c.post(f'/api/quick/rows/{rid}/gmail').json()
    assert made['status']=='Gmail 초안' and made['sent'] is False and made['gmail_draft_id']=='draft-1'
    msg=decode_raw(gmail.drafts[-1][1])
    assert msg['To']=='tax@example.com' and [a.get_filename() for a in msg.iter_attachments()]==['소개서.pdf']
    c.post(f'/api/quick/rows/{rid}/gmail');assert gmail.drafts[-1][0]=='draft-1'
    c.post(f'/api/quick/rows/{nomail}/draft')
    assert c.post(f'/api/quick/rows/{nomail}/gmail').status_code==400
    edited=c.patch(f'/api/quick/rows/{nomail}',json={'email':' new@example.com '}).json()
    assert edited['email']=='new@example.com'
    assert c.delete(f'/api/quick/rows/{nomail}').status_code==200 and len(c.get('/api/quick/state').json()['rows'])==1


def test_without_ai_key_search_says_so(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.delenv('OPENAI_API_KEY',raising=False);monkeypatch.delenv('SALES_AX_PUBLIC_HOST',raising=False)
    with TestClient(create_app(tmp_path/'n.db')) as c:
        assert c.get('/api/quick/state').json()['ai_configured'] is False
        r=c.post('/api/quick/search',json={'query':'세무사'})
        assert r.status_code==503 and 'OPENAI_API_KEY' in r.json()['detail']


def test_gmail_sent_import_creates_leads_and_replies(tmp_path):
    from types import SimpleNamespace
    from ax.db import DB
    from ax.service import Workflow
    from ax.gmail_import import import_sent
    msgs={'m1':{'id':'m1','threadId':'t1','internalDate':'1761800000000','snippet':'안녕하세요','labelIds':['SENT'],
               'payload':{'headers':[{'name':'To','value':'"홍길동" <hong@shop.co.kr>'},{'name':'From','value':'marketing@creatobi.com'},{'name':'Subject','value':'제안'},{'name':'Message-ID','value':'<m1@x>'}]}},
          'm2':{'id':'m2','threadId':'t2','internalDate':'1761800000000','labelIds':['SENT'],
               'payload':{'headers':[{'name':'To','value':'a@x.com, b@y.com'},{'name':'From','value':'marketing@creatobi.com'}]}},
          'm3':{'id':'m3','threadId':'t3','internalDate':'1761800000000','labelIds':['SENT'],
               'payload':{'headers':[{'name':'To','value':'team@creatobi.com'},{'name':'From','value':'marketing@creatobi.com'}]}}}
    reply={'id':'r1','threadId':'t1','internalDate':'1761900000000','labelIds':['INBOX'],'snippet':'관심 있어요',
           'payload':{'headers':[{'name':'From','value':'hong@shop.co.kr'},{'name':'To','value':'marketing@creatobi.com'},{'name':'In-Reply-To','value':'<m1@x>'}]}}
    class Req:
        def __init__(self,v):self.v=v
        def execute(self):return self.v
    class Messages:
        def list(self,**kw):return Req({'messages':[{'id':k,'threadId':v['threadId']} for k,v in msgs.items()]})
        def get(self,userId,id,**kw):return Req(msgs[id])
    service=SimpleNamespace(users=lambda:SimpleNamespace(messages=lambda:Messages()))
    client=SimpleNamespace(service=service,account=lambda:'marketing@creatobi.com',thread_messages=lambda t:[msgs.get('m1'),reply] if t=='t1' else [])
    if True:
        wf=Workflow(DB(tmp_path/'g.db'),lambda:client)
        first=import_sent(wf,client)
        assert (first['sent_imported'],first['replies_imported'],first['leads_created'],first['skipped'])==(1,1,1,2)
        lead=wf.db.one('SELECT l.stage,c.email FROM leads l JOIN contacts c ON c.id=l.contact_id')
        assert lead['email']=='hong@shop.co.kr' and lead['stage']=='IN_CONVERSATION'
        again=import_sent(wf,client)
        assert (again['sent_imported'],again['replies_imported'],again['leads_created'])==(0,0,0)
