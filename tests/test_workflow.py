import base64
import io
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from ax.api import create_app
from ax.gmail import mime_message, verify_raw, decode_raw


class FakeGmail:
    def __init__(self): self.raw={}; self.creates=0; self.updates=0; self.corrupt=False; self.fail=False
    def account(self): return 'marketing@creatobi.com'
    def upsert(self,draft_id,raw,thread_id=''):
        if self.fail: raise TimeoutError('ambiguous network failure')
        if draft_id: self.updates+=1
        else: self.creates+=1
        gid=draft_id or f'draft-{self.creates}'
        self.raw[gid]=raw
        return {'id':gid}
    def read(self,gid):
        if self.corrupt:
            return mime_message('marketing@creatobi.com','wrong@example.com','x','x')
        return self.raw[gid]
    def thread_messages(self,tid): return []


@pytest.fixture
def env(tmp_path):
    gmail=FakeGmail(); app=create_app(tmp_path/'test.db',lambda:gmail)
    with TestClient(app) as c:
        campaign=c.post('/api/campaigns',json={'name':'통합 테스트','product':'전문가 공동 교육','value_prop':'노하우를 AI 시스템으로 연결','industries':['세무']}).json()
        yield c,app.state.workflow,gmail,campaign['id']


def add(env,email='kim@example.com',name='김대표',campaign=None,verify=True):
    c,w,g,cid=env
    r=c.post('/api/leads',json={'campaign_id':campaign or cid,'name':name,'company':'모범 세무','domain':'example.com','email':email,'source_url':'https://example.com/team'})
    assert r.status_code==200,r.text
    lid=r.json()['id']
    if verify:
        assert c.post(f'/api/leads/{lid}/verify',json={'confirmed':True,'source_url':'https://example.com/team'}).status_code==200
    return lid


def pdf():
    out=io.BytesIO(); w=PdfWriter(); w.add_blank_page(width=600,height=800); w.write(out); return out.getvalue()


def draft(env,lid,body='김대표님, 안녕하세요. 협업을 제안드립니다.',**kwargs):
    return env[0].post(f'/api/leads/{lid}/draft',json={'subject':'협업 제안','body':body,**kwargs})


def sent(env,lid,gid='sent-1',tid='thread-1'):
    return env[0].post('/api/messages/import',json={'account':'marketing@creatobi.com','gmail_id':gid,'thread_id':tid,'rfc_id':f'<{gid}@creatobi.com>',
        'from':'marketing@creatobi.com','to':env[1].lead(lid)['email'],'lead_id':lid,'subject':'협업 제안','occurred_at':'2026-10-01T10:00:00+09:00'})


def test_contact_identity_cross_campaign_and_same_org_people(env):
    c,w,g,cid=env
    a=add(env,email=' KIM@EXAMPLE.COM ')
    b=add(env)
    assert a==b
    cid2=c.post('/api/campaigns',json={'name':'다른 캠페인','product':'교육','value_prop':'가치'}).json()['id']
    c2=add(env,campaign=cid2)
    other=add(env,email='lee@example.com',name='이대표')
    assert w.lead(a)['contact_id']==w.lead(c2)['contact_id']
    assert w.lead(a)['contact_id']!=w.lead(other)['contact_id']
    assert w.funnel()['contacts']==2 and w.funnel()['leads']==3


def test_identity_uncertainty_holds_not_merges(env):
    a=add(env)
    b=add(env,email='second@example.com',verify=False)
    assert a!=b and env[1].lead(b)['stage']=='QA_HOLD'


def test_cannot_draft_unverified_or_multiple_recipients(env):
    lid=add(env,verify=False)
    assert draft(env,lid).status_code==400
    assert env[0].post('/api/leads',json={'campaign_id':env[3],'name':'A','company':'X','email':'a@x.com,b@y.com'}).status_code==400


def test_draft_upsert_resets_approval_and_gmail_updates(env):
    c,w,g,cid=env; lid=add(env)
    d=draft(env,lid).json()
    assert c.post(f"/api/drafts/{d['id']}/approve",json={'confirmed':True,'actor':'담당자','version':1}).status_code==400
    assert c.post(f"/api/drafts/{d['id']}/gmail").status_code==200
    assert c.post(f"/api/drafts/{d['id']}/approve",json={'confirmed':True,'actor':'담당자','version':1}).status_code==200
    updated=draft(env,lid,body='김대표님, 수정 제안드립니다.').json()
    assert updated['id']==d['id'] and updated['version']==2 and updated['approved_at'] is None
    assert c.post(f"/api/drafts/{d['id']}/approve",json={'confirmed':True,'actor':'담당자','version':1}).status_code==400
    assert c.post(f"/api/drafts/{d['id']}/gmail").status_code==200
    assert g.creates==1 and g.updates==1
    assert w.funnel()['contacted']==0


def test_pdf_mime_and_cross_customer_rejection(env):
    c,w,g,cid=env; a=add(env); b=add(env,email='lee@example.com',name='이대표')
    pid=c.post(f'/api/leads/{a}/proposal',files={'file':('kim.pdf',pdf(),'application/pdf')},data={'identity_checked':'true'}).json()['id']
    assert draft(env,b,body='이대표님, 첨부드립니다.',proposal_id=pid).status_code==400
    d=draft(env,a,body='김대표님, 첨부드립니다.',proposal_id=pid).json()
    assert c.post(f"/api/drafts/{d['id']}/gmail").status_code==200
    msg=decode_raw(next(iter(g.raw.values())))
    assert msg['To']=='kim@example.com' and not msg['Cc'] and not msg['Bcc']
    attachment=list(msg.iter_attachments())[0]
    assert attachment.get_filename()=='kim.pdf' and attachment.get_payload(decode=True)==pdf()


def test_missing_attachment_and_unchecked_pdf_hold(env):
    c,w,g,cid=env; lid=add(env)
    d=draft(env,lid,body='김대표님, 첨부드립니다.').json()
    assert d['status']=='QA_HOLD'
    pid=c.post(f'/api/leads/{lid}/proposal',files={'file':('kim.pdf',pdf(),'application/pdf')},data={'identity_checked':'false'}).json()['id']
    assert draft(env,lid,body='김대표님, 첨부드립니다.',proposal_id=pid).json()['status']=='QA_HOLD'


def test_unverified_numbers_and_wrong_name_blocked(env):
    c,w,g,cid=env; lid=add(env); add(env,email='lee@example.com',name='이대표')
    assert draft(env,lid,body='김대표님, 매출 300% 성장 사례를 확인했습니다.').json()['status']=='QA_HOLD'
    assert draft(env,lid,body='김대표님과 이대표님 안녕하세요.').json()['status']=='QA_HOLD'
    c.post(f'/api/leads/{lid}/evidence',json={'claim':'매출 300% 성장 사례','source_url':'https://example.com/case','human_checked':True})
    assert draft(env,lid,body='김대표님\n매출 300% 성장 사례').json()['status']=='QA_PASS'


def test_gmail_re_read_mismatch_and_ambiguous_create_stop(env):
    c,w,g,cid=env; lid=add(env); did=draft(env,lid).json()['id']; g.corrupt=True
    assert c.post(f'/api/drafts/{did}/gmail').status_code==400
    assert w.draft(did)['sync_state']=='UNCERTAIN'
    assert c.post(f'/api/drafts/{did}/gmail').status_code==400
    assert g.creates==1
    assert c.post(f'/api/drafts/{did}/approve',json={'confirmed':True,'actor':'A','version':1}).status_code==400


def test_network_timeout_does_not_retry_create(env):
    c,w,g,cid=env; lid=add(env); did=draft(env,lid).json()['id'];g.fail=True
    assert c.post(f'/api/drafts/{did}/gmail').status_code==400
    g.fail=False
    assert c.post(f'/api/drafts/{did}/gmail').status_code==400
    assert g.creates==0 and w.draft(did)['sync_state']=='UNCERTAIN'


def test_thread_match_is_exact_and_idempotent(env):
    c,w,g,cid=env; lid=add(env)
    assert sent(env,lid).status_code==200
    reply={'account':'marketing@creatobi.com','gmail_id':'reply-1','thread_id':'thread-1','from':'kim@example.com','to':'marketing@creatobi.com','intent':'HUMAN','occurred_at':'2026-10-02T11:00:00+09:00'}
    wrong={**reply,'thread_id':'unrelated','gmail_id':'unrelated'}
    assert c.post('/api/messages/import',json=wrong).status_code==400
    assert c.post('/api/messages/import',json=reply).json()['created'] is True
    assert c.post('/api/messages/import',json=reply).json()['created'] is False
    assert w.funnel()['contacted']==1 and w.funnel()['replied']==1
    assert w.db.one("SELECT count(*) n FROM tasks WHERE status='open'")['n']==0


def test_rfc_fallback_and_wrong_sender_rejected(env):
    c,w,g,cid=env; lid=add(env);sent(env,lid)
    reply={'account':'marketing@creatobi.com','gmail_id':'rfc-reply','thread_id':'different','in_reply_to':'<sent-1@creatobi.com>','from':'kim@example.com','to':'marketing@creatobi.com','occurred_at':'2026-10-02T11:00:00+09:00'}
    assert c.post('/api/messages/import',json=reply).status_code==200
    assert c.post('/api/messages/import',json={**reply,'gmail_id':'wrong','from':'bad@example.com'}).status_code==400


def test_suppression_applies_across_campaigns_and_followups(env):
    c,w,g,cid=env; lid=add(env);sent(env,lid)
    c.post(f'/api/leads/{lid}/suppress',json={'confirmed':True,'actor':'A','notes':'거절'})
    assert draft(env,lid,step=1).status_code==400
    other=c.post('/api/campaigns',json={'name':'다른 캠페인','product':'교육','value_prop':'협업'}).json()['id']
    lid2=add(env,campaign=other)
    assert draft(env,lid2).status_code==400


def test_same_contact_different_campaign_cannot_restart_intro(env):
    c,w,g,cid=env; lid=add(env);sent(env,lid)
    other=c.post('/api/campaigns',json={'name':'다른 캠페인','product':'교육','value_prop':'협업'}).json()['id']
    lid2=add(env,campaign=other)
    assert draft(env,lid2).json()['status']=='QA_HOLD'


def test_followup_keeps_thread_and_cadence(env):
    c,w,g,cid=env;lid=add(env);sent(env,lid)
    task=w.db.one('SELECT * FROM tasks WHERE lead_id=?',(lid,))
    assert task['kind']=='followup_email' and task['step']==1
    d=draft(env,lid,step=1).json()
    assert d['subject']=='Re: 협업 제안' and d['in_reply_to']=='<sent-1@creatobi.com>' and d['thread_id']=='thread-1'


def test_booked_is_not_completed_even_in_past_and_event_dedupes(env):
    c,w,g,cid=env;lid=add(env)
    event={'lead_id':lid,'starts_at':'2026-01-01T12:00:00+09:00','external_id':'calendar-1'}
    a=c.post('/api/meetings',json=event).json();b=c.post('/api/meetings',json=event).json()
    assert a['id']==b['id'] and w.funnel()['completed']==0 and w.funnel()['booked']==1
    path=f"/api/meetings/{a['id']}/complete"
    assert c.post(path,json={'confirmed':True,'actor':'A','notes':''}).status_code==400
    assert c.post(path,json={'confirmed':True,'actor':'담당자','notes':'실제 상담 완료','next_step':'자료 검토'}).status_code==200
    assert w.funnel()['completed']==1


def test_deal_values_survive_repeated_sync(env):
    c,w,g,cid=env; lid=add(env)
    assert c.post(f'/api/leads/{lid}/deal',json={'stage':'PAID','amount_krw':1500000,'evidence':'입금 내역','actor':'담당자','confirmed':True}).status_code==200
    sent(env,lid);sent(env,lid)
    assert w.funnel()['revenue']==1500000


def test_send_is_disabled_even_after_approval(env):
    c,w,g,cid=env;lid=add(env);did=draft(env,lid).json()['id']
    c.post(f'/api/drafts/{did}/gmail')
    assert c.post(f'/api/drafts/{did}/approve',json={'confirmed':True,'actor':'A','version':1}).status_code==200
    assert c.post(f'/api/drafts/{did}/send').status_code==403
    assert not hasattr(g,'send')


def test_legacy_qualification_and_weak_stage_preserved(env):
    c,w,g,cid=env;lid=add(env)
    a=c.post(f'/api/leads/{lid}/qualify',json={'score':45,'fit':'weak','rationale':'규모 미확인'}).json()
    assert a['stage']=='VERIFIED'
    b=c.post(f'/api/leads/{lid}/qualify',json={'score':85,'fit':'strong','rationale':'ICP 충족'}).json()
    assert b['stage']=='QUALIFIED'


def test_origin_protection_and_web_shell(env):
    c,w,g,cid=env
    assert c.get('/').status_code==200
    assert c.get('/static/app.js').status_code==200
    assert c.post('/api/campaigns',headers={'Origin':'https://evil.example'},json={'name':'x','product':'x','value_prop':'x'}).status_code==403
    assert c.get('/api/status',headers={'Host':'evil.example'}).status_code==403


def test_mime_header_injection_rejected():
    with pytest.raises(ValueError):mime_message('a@example.com','b@example.com','hello\nBcc: bad@example.com','body')


def test_parallel_import_has_single_identity(env):
    c,w,g,cid=env
    data={'campaign_id':cid,'name':'동시고객','company':'동시회사','email':'race@example.com','domain':'race.example.com'}
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:w.add_lead(data),range(8)))
    assert sum(r['created'] for r in results)==1
