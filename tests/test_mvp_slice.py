import io
from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from ax.api import create_app


class Gmail:
    def __init__(self):self.raw='';self.sends=0;self.fail=False
    def account(self):return 'marketing@creatobi.com'
    def upsert(self,did,raw,thread_id=''):self.raw=raw;return {'id':'draft-test'}
    def read(self,did):return self.raw
    def send_draft(self,did):
        self.sends+=1
        if self.fail:raise TimeoutError()
        return {'id':'sent-test','threadId':'thread-test'}


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.setenv('SALES_AX_SEND_ENABLED','true')
    monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False)
    gmail=Gmail();app=create_app(tmp_path/'test.db',lambda:gmail)
    with TestClient(app) as c:yield c,app,gmail


def ready(env,with_pdf=True):
    c,app,g=env
    cid=c.post('/api/campaigns',json={'name':'테스트','product':'공동 교육','value_prop':'협업'}).json()['id']
    lid=c.post('/api/leads',json={'campaign_id':cid,'name':'테스트고객','company':'Test','email':'person@example.com'}).json()['id']
    c.post(f'/api/leads/{lid}/verify',json={'confirmed':True,'source_url':'https://example.com/contact'})
    pid=None
    if with_pdf:
        w=PdfWriter();w.add_blank_page(width=200,height=200);o=io.BytesIO();w.write(o)
        pid=c.post(f'/api/leads/{lid}/proposal',files={'file':('proposal.pdf',o.getvalue(),'application/pdf')},data={'identity_checked':'true'}).json()['id']
    d=c.post(f'/api/leads/{lid}/draft',json={'subject':'협업 제안','body':'테스트고객님, 협업을 제안드립니다.','proposal_id':pid}).json()
    assert c.post(f"/api/drafts/{d['id']}/gmail").status_code==200
    return d


def schedule(env,d):
    return env[0].post(f"/api/drafts/{d['id']}/schedule",json={'version':d['version'],'recipient':'person@example.com','scheduled_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'actor':'검수자','confirmed':True})


def test_customer_definition_is_versioned_and_not_fake_ai(env):
    c,app,g=env
    sid=c.post('/api/personas').json()['id']
    body={'text':'중고명품 리셀 대표에게 AI 교육을 제안하고 싶어','request_id':'same-request-001'}
    a=c.post(f'/api/personas/{sid}/messages',json=body).json()
    b=c.post(f'/api/personas/{sid}/messages',json=body).json()
    assert a['version']==b['version']==1 and a['provider']=='RULE_BASED_MANUAL'
    approved=c.post(f'/api/personas/{sid}/approve',json={'version':1,'confirmed':True}).json()
    assert approved['campaign_id']
    assert c.post(f'/api/personas/{sid}/approve',json={'version':1,'confirmed':True}).json()['campaign_id']==approved['campaign_id']
    card=approved['card'];card['name']='수정한 캠페인'
    edited=c.put(f'/api/personas/{sid}/card',json=card).json()
    assert edited['version']==2 and edited['approved_version'] is None
    assert c.post(f'/api/personas/{sid}/approve',json={'version':1,'confirmed':True}).status_code==400


def test_unapproved_or_missing_instructor_pdf_cannot_schedule(env):
    c,app,g=env;d=ready(env,False)
    assert schedule(env,d).status_code==400
    c.post(f"/api/drafts/{d['id']}/approve",json={'version':1,'actor':'담당자','confirmed':True})
    assert schedule(env,d).status_code==400 and g.sends==0


def test_approved_schedule_sends_once_and_does_not_claim_delivery(env):
    c,app,g=env;d=ready(env)
    c.post(f"/api/drafts/{d['id']}/approve",json={'version':1,'actor':'담당자','confirmed':True})
    s=schedule(env,d);assert s.status_code==200,s.text
    assert schedule(env,d).json()['id']==s.json()['id']
    wf=app.state.workflow
    with wf.db.transaction():wf.db.execute("UPDATE scheduled_deliveries SET scheduled_at='2026-01-01T00:00:00+00:00' WHERE id=?",(s.json()['id'],))
    app.state.delivery.dispatch(s.json()['id']);app.state.delivery.dispatch(s.json()['id'])
    assert g.sends==1 and wf.funnel()['contacted']==1


def test_ambiguous_send_never_retries(env):
    c,app,g=env;d=ready(env)
    c.post(f"/api/drafts/{d['id']}/approve",json={'version':1,'actor':'담당자','confirmed':True})
    s=schedule(env,d).json();g.fail=True
    with app.state.workflow.db.transaction():app.state.workflow.db.execute("UPDATE scheduled_deliveries SET scheduled_at='2026-01-01T00:00:00+00:00' WHERE id=?",(s['id'],))
    assert app.state.delivery.dispatch(s['id'])['status']=='UNCERTAIN'
    app.state.delivery.dispatch(s['id'])
    assert g.sends==1 and app.state.workflow.funnel()['contacted']==0
