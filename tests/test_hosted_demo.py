import base64
import pytest
from fastapi.testclient import TestClient
from ax.api import create_app

HOST='sales-ax-demo.up.railway.app'
PASSWORD='correct-horse-battery'


def basic(password):
    return {'Authorization':'Basic '+base64.b64encode(('demo:'+password).encode()).decode()}


@pytest.fixture
def hosted(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.setenv('SALES_AX_PUBLIC_HOST',HOST)
    monkeypatch.setenv('SALES_AX_DEMO_PASSWORD',PASSWORD)
    with TestClient(create_app(tmp_path/'app.db'),base_url='https://'+HOST) as client:yield client


def test_hosted_demo_requires_password_everywhere(hosted):
    for path in ('/','/api/status','/api/campaigns','/docs','/static/app.js'):
        r=hosted.get(path)
        assert r.status_code==401 and r.headers['www-authenticate'].startswith('Basic')
    assert hosted.get('/api/status',headers=basic('wrong-password-123')).status_code==401
    assert hosted.get('/api/status',headers={'Authorization':'Basic !!!'}).status_code==401
    ok=hosted.get('/api/status',headers=basic(PASSWORD))
    assert ok.status_code==200 and ok.json()['mode']=='HOSTED_DEMO' and ok.json()['send_enabled'] is False


def test_hosted_demo_blocks_google_and_foreign_origins(hosted):
    auth=basic(PASSWORD)
    assert hosted.post('/api/integrations/google/start',headers=auth).status_code==403
    assert hosted.get('/api/integrations/google/callback?code=x&state=y',headers=auth).status_code==403
    body={'name':'demo','product':'AI 교육','value_prop':'업무 개선'}
    assert hosted.post('/api/campaigns',json=body,headers={**auth,'Origin':'https://evil.example'}).status_code==403
    assert hosted.post('/api/campaigns',json=body,headers={**auth,'Origin':'https://'+HOST}).status_code==200


def test_hosted_demo_rejects_other_hosts_and_weak_password(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.setenv('SALES_AX_PUBLIC_HOST',HOST)
    monkeypatch.setenv('SALES_AX_DEMO_PASSWORD','short')
    with pytest.raises(RuntimeError):create_app(tmp_path/'weak.db')
    monkeypatch.setenv('SALES_AX_DEMO_PASSWORD',PASSWORD)
    with TestClient(create_app(tmp_path/'app.db'),base_url='https://attacker.example') as c:
        assert c.get('/api/status',headers=basic(PASSWORD)).status_code==403


def test_local_mode_unchanged(tmp_path,monkeypatch):
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.delenv('SALES_AX_PUBLIC_HOST',raising=False)
    with TestClient(create_app(tmp_path/'app.db')) as c:
        assert c.get('/api/status').json()['mode']=='LOCAL_SINGLE_OPERATOR'


def test_seed_snapshot_loads_once_on_cold_start(tmp_path,monkeypatch):
    from test_crm_import import PIPELINE,HISTORY
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.delenv('SALES_AX_PUBLIC_HOST',raising=False)
    monkeypatch.setenv('SALES_AX_SEED_PIPELINE_B64',base64.b64encode(PIPELINE.encode()).decode())
    monkeypatch.setenv('SALES_AX_SEED_HISTORY_B64',base64.b64encode(HISTORY.encode()).decode())
    for _ in range(2):
        with TestClient(create_app(tmp_path/'seed.db',lambda:(_ for _ in ()).throw(ValueError('no gmail')))) as c:
            assert len(c.get('/api/campaigns').json())==1
            leads=c.get('/api/leads').json()
            assert sorted(l['name'] for l in leads)==['가상고객','이메일없는고객']
