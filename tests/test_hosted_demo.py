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


# Fictional rows shaped like the real exports (세무사 70명 tab, 파트너지원 landing form). No real customer data.
TAX='''번호,회사,대상,이메일,전문분야,개인화근거,맞춤협업가설,발송준비상태,중복발송상태,출처등급,근거URL,제목,본문,첨부 PDF,Gmail 초안,진행 상태
1,가상세무법인,김가상,tax@example.com,쇼핑몰,"근거
여러 줄",가설,초안 작성 가능,신규,official,https://example.com,제목,"본문
여러 줄",,,초안 완료
,,,,,,,,,,,,,,,
'''
PARTNER='''접수 시각,이름,연락처,전문 업종,실무 경력,채널·포트폴리오 링크,한 줄 소개,개인정보 동의,광고성 정보 수신 동의,유입 페이지
2026-10-21 1:44:01,[테스트] 연동확인,01000000000,기타,,,테스트,동의,미동의,local-test
2026-10-21 9:23:11,이가상,010-1111-2222,학원·교육,3년 미만,https:// www.example.org/,,동의,미동의,landing
2026-10-26 17:52:14,이가상,01011113333,학원·교육,3년 미만,,"두 줄
소개",동의,동의,landing
2026-10-28 16:31:59,E2E 점검 (삭제 예정),01000000000,기타,,,,동의,미동의,landing
'''


def test_seed_builds_two_campaigns_and_loads_once_on_cold_start(tmp_path,monkeypatch):
    from ax.seed import tax_campaign,partner_campaign,encode
    campaigns=[tax_campaign(TAX),partner_campaign(PARTNER)]
    assert [len(c['pipeline']) for c in campaigns]==[1,1]
    partner=campaigns[1]['pipeline'][0]
    assert partner['리드ID']=='PTN-20261026175214' and partner['한 줄 소개']=='두 줄 소개' and '연락처' not in partner
    monkeypatch.setenv('SALES_AX_PRIVATE_DIR',str(tmp_path/'private'))
    monkeypatch.delenv('SALES_AX_PUBLIC_HOST',raising=False)
    monkeypatch.setenv('SALES_AX_SEED_GZ_B64',encode(campaigns))
    for _ in range(2):
        with TestClient(create_app(tmp_path/'seed.db',lambda:(_ for _ in ()).throw(ValueError('no gmail')))) as c:
            assert len(c.get('/api/campaigns').json())==2
            assert sorted(l['name'] for l in c.get('/api/leads').json())==['김가상','이가상']
