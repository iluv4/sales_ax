import io
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from ax.api import create_app
from ax.oauth import GoogleConnection
from ax.google_readonly import sync_calendar


@pytest.fixture
def app_env(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    app=create_app(tmp_path/'app.db',gmail_factory=lambda: (_ for _ in ()).throw(ValueError('not configured')))
    with TestClient(app) as client:
        cid=client.post('/api/campaigns',json={'name':'test','product':'AI 교육','value_prop':'업무 개선'}).json()['id']
        lid=client.post('/api/leads',json={'campaign_id':cid,'name':'테스트고객','company':'Test Company','email':'person@example.com'}).json()['id']
        yield client,app,lid


def config():
    return {'web':{'client_id':'test.apps.googleusercontent.com','client_secret':'not-a-real-secret',
        'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token',
        'redirect_uris':['http://127.0.0.1:8765/api/integrations/google/callback']}}


def test_health_does_not_fake_connected(app_env):
    c,app,lid=app_env
    health=c.get('/api/integrations/health').json()
    assert not health['google']['token_stored']
    assert next(x for x in health['providers'] if x['provider']=='gmail')['state']=='BLOCKED'
    assert c.post('/api/integrations/google/check').json()['results']=={'gmail':'BLOCKED','calendar':'BLOCKED'}
    assert c.post('/api/integrations/calendar/sync').status_code==503


def test_client_upload_is_encrypted_and_rejects_non_google(app_env):
    c,app,lid=app_env
    r=c.post('/api/integrations/google/client',files={'file':('client.json',json.dumps(config()),'application/json')})
    assert r.status_code==200 and r.json()['secrets_returned'] is False
    content=app.state.google.client_path.read_bytes()
    assert b'not-a-real-secret' not in content and b'client_secret' not in content
    bad=config();bad['web']['token_uri']='https://evil.example/token'
    assert c.post('/api/integrations/google/client',files={'file':('client.json',json.dumps(bad),'application/json')}).status_code==400


def test_oauth_state_cookie_account_and_replay(tmp_path):
    class FakeFlow:
        credentials=SimpleNamespace(to_json=lambda:json.dumps({'token':'dummy-token','refresh_token':'dummy-refresh'}))
        def authorization_url(self,**kw):return 'https://accounts.google.com/o/oauth2/auth?state=test-state','test-state'
        def fetch_token(self,code):assert code=='dummy-code'
    conn=GoogleConnection(tmp_path,flow_factory=lambda *a,**k:FakeFlow(),account_reader=lambda c:'marketing@creatobi.com')
    conn.configure(json.dumps(config()))
    url,cookie=conn.begin()
    assert 'accounts.google.com' in url
    assert conn.finish('test-state','dummy-code',cookie)=='marketing@creatobi.com'
    assert b'dummy-token' not in conn.token_path.read_bytes()
    assert conn.status()['token_stored'] and not conn.status()['live_verified']
    with pytest.raises(ValueError):conn.finish('test-state','dummy-code',cookie)


def test_oauth_bad_cookie_and_wrong_account_never_store(tmp_path):
    class FakeFlow:
        credentials=SimpleNamespace(to_json=lambda:'{}')
        def authorization_url(self,**kw):return 'https://accounts.google.com/auth','s'
        def fetch_token(self,code):pass
    conn=GoogleConnection(tmp_path,flow_factory=lambda *a,**k:FakeFlow(),account_reader=lambda c:'wrong@example.com')
    conn.configure(json.dumps(config()))
    _,cookie=conn.begin()
    with pytest.raises(ValueError):conn.finish('s','x','wrong-cookie')
    assert not conn.token_path.exists()
    _,cookie=conn.begin()
    with pytest.raises(ValueError):conn.finish('s','x',cookie)
    assert not conn.token_path.exists()


def test_calendar_readonly_dedupes_and_never_marks_completed(app_env):
    c,app,lid=app_env
    class FakeCalendar:
        def events(self):return self
        def list(self,**kw):
            assert 'timeMin' in kw and 'timeMax' in kw
            return self
        def execute(self):return {'items':[{'id':'calendar-1','start':{'dateTime':'2026-01-01T10:00:00+09:00'},'attendees':[{'email':'person@example.com'}],'status':'confirmed'},
                                           {'id':'unmatched','start':{'dateTime':'2026-01-02T10:00:00+09:00'},'summary':'unmatched','status':'confirmed'}]}
    wf=app.state.workflow
    assert sync_calendar(wf,FakeCalendar())['created']==1
    assert sync_calendar(wf,FakeCalendar())['created']==0
    assert wf.funnel()['completed']==0 and wf.funnel()['booked']==1
    assert len(c.get('/api/integrations/calendar/unmatched').json())==1


def test_featpaper_manual_unknown_mock_exclusion_and_idempotency(app_env):
    c,app,lid=app_env
    w=PdfWriter();w.add_blank_page(width=200,height=200);out=io.BytesIO();w.write(out)
    pid=c.post(f'/api/leads/{lid}/proposal',files={'file':('proposal.pdf',out.getvalue(),'application/pdf')},data={'identity_checked':'true'}).json()['id']
    link={'lead_id':lid,'proposal_id':pid,'url':'https://featpaper.com/example-recipient-link'}
    a=c.post('/api/document-links',json=link).json();b=c.post('/api/document-links',json=link).json()
    assert a['id']==b['id']
    assert c.get('/api/analytics/documents').json()['links'][0]['view_state']=='unknown'
    event={'link_id':a['id'],'confirmed':True,'observation_source':'https://featpaper.com/dashboard/example','occurred_at':'2026-11-01T10:00:00+09:00','external_event_id':'event-1','is_mock':True}
    assert c.post('/api/document-events/observe',json=event).json()['created']
    assert not c.post('/api/document-events/observe',json=event).json()['created']
    assert c.get('/api/analytics/documents').json()['links'][0]['view_state']=='unknown'
    assert c.get('/api/analytics/documents?demo=true').json()['links'][0]['view_state']=='observed'
    event.update(is_mock=False,external_event_id='real-observation')
    assert c.post('/api/document-events/observe',json=event).status_code==200
    real=c.get('/api/analytics/documents').json()['links'][0]
    assert real['view_state']=='observed' and real['unique_visits'] is None and real['completion']=='unknown'


def test_google_callback_missing_state_cannot_connect(app_env):
    c,app,lid=app_env
    r=c.get('/api/integrations/google/callback?state=bad&code=redacted-test-code')
    assert r.status_code==400
    assert 'redacted-test-code' not in r.text and not app.state.google.token_path.exists()
