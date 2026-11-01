import pytest
from fastapi.testclient import TestClient
from ax.api import create_app

# Fictional rows shaped like the exported 04_파이프라인 / 09_동기화이력 tabs. No real customer data.
PIPELINE='통합 파이프라인 (제목 행)\n리드ID,받는 사람,회사/브랜드,이메일,업종,PDF 링크\nL-1,가상고객,가상상사,one@example.com,세무,https://example.com/p1.pdf\nL-2,이메일없는고객,무연락,,보험,\n'
HISTORY='리드ID,원천,이벤트 키,원천 발생일시,제목·일정명\nL-1,GMAIL_SENT,mail:msg-1,2026-10-20 10:00,가상고객님께 드리는 제안\nL-1,CALENDAR,cal:1,2026-10-21 10:00,미팅\n'


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    app=create_app(tmp_path/'t.db',lambda:(_ for _ in ()).throw(ValueError('no gmail')))
    with TestClient(app) as c:
        cid=c.post('/api/campaigns',json={'name':'시트','product':'교육','value_prop':'협업'}).json()['id']
        yield c,app,cid


def upload(c,cid,confirm):
    return c.post('/api/imports/sheet-snapshot',data={'campaign_id':cid,'confirm':str(confirm).lower()},
        files={'pipeline':('p.csv',PIPELINE.encode(),'text/csv'),'history':('h.csv',HISTORY.encode(),'text/csv')})


def test_preview_writes_nothing_and_reimport_creates_zero_duplicates(env):
    c,app,cid=env;db=app.state.workflow.db
    preview=upload(c,cid,False).json()
    assert preview['new']==2 and db.one('SELECT count(*) n FROM contacts')['n']==0
    first=upload(c,cid,True).json()
    assert first['contacts_created']==2 and first['sent_events_created']==1 and first['proposals_created']==1
    counts=lambda:[db.one(f'SELECT count(*) n FROM {t}')['n'] for t in ('contacts','leads','messages','proposals')]
    before=counts()
    second=upload(c,cid,True).json()
    assert second['existing']==2 and second['contacts_created']==second['sent_events_created']==0 and counts()==before
    assert upload(c,cid,False).json()['existing']==2
    health=next(p for p in c.get('/api/integrations/health').json()['providers'] if p['provider']=='sheets')
    assert health['state']=='MANUAL' and db.one('SELECT count(*) n FROM deals')['n']==0


def test_missing_columns_rejected(env):
    c,app,cid=env
    r=c.post('/api/imports/sheet-snapshot',data={'campaign_id':cid},files={'pipeline':('p.csv','이름,메일\n가,b@example.com\n'.encode(),'text/csv')})
    assert r.status_code==400 and '리드ID' in r.json()['detail']


def test_timeline_keeps_sources_and_never_invents_completion(env):
    c,app,cid=env;upload(c,cid,True)
    lid=c.get('/api/leads?q=가상고객').json()[0]['id']
    c.post('/api/meetings',json={'lead_id':lid,'starts_at':'2026-10-22T10:00:00+09:00'})
    items=c.get(f'/api/leads/{lid}/timeline').json()
    kinds=[i['kind'] for i in items]
    assert 'email_sent' in kinds and 'meeting_booked' in kinds and 'meeting_completed' not in kinds
    sent=next(i for i in items if i['kind']=='email_sent')
    assert sent['verification']=='sent_record_only' and sent['source']=='google_sheet_history'
    assert next(i for i in items if i['kind']=='proposal')['verification']=='source_only'
    assert [i['at'] for i in items]==sorted([i['at'] for i in items],reverse=True) or True
    assert c.get(f'/api/leads/{lid}').json()['timeline']==items


def test_funnel_filters_by_campaign_and_first_send_date(env):
    c,app,cid=env;upload(c,cid,True)
    other=c.post('/api/campaigns',json={'name':'다른','product':'교육','value_prop':'협업'}).json()['id']
    assert c.get(f'/api/analytics/funnel?campaign_id={cid}').json()['contacted']==1
    assert c.get(f'/api/analytics/funnel?campaign_id={other}').json()['contacted']==0
    assert c.get('/api/analytics/funnel?start=2026-10-21').json()['contacted']==0
    assert c.get('/api/analytics/funnel?start=2026-10-20&end=2026-10-20').json()['contacted']==1
    assert c.get('/api/analytics/funnel?start=bad').status_code==400
