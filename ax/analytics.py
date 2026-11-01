"""Contact cohorts use first real outbound timestamp; all subsequent milestones belong to cohort."""
def funnel(db,campaign_id=None,industry=None,from_date=None,to_date=None):
    leads=db.rows('SELECT l.*,c.org_id,o.industry FROM leads l JOIN contacts c ON c.id=l.contact_id LEFT JOIN organizations o ON o.id=c.org_id')
    leads=[l for l in leads if (campaign_id is None or l['campaign_id']==campaign_id) and (not industry or l['industry']==industry)]
    messages=db.rows('SELECT * FROM messages WHERE is_mock=0')
    outbound=[m for m in messages if m['direction']=='OUT']
    by_contact={}
    from datetime import datetime,date,timedelta,timezone
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('Asia/Seoul')
    def bound(text,end=False):
        if not text:return None
        return datetime.combine(date.fromisoformat(text)+(timedelta(days=1) if end else timedelta()),datetime.min.time(),tz)
    lo,hi=bound(from_date),bound(to_date,True)
    for lead in leads:
        sent=[m for m in outbound if m['lead_id']==lead['id']]
        if not sent:continue
        first=min(datetime.fromisoformat(m['occurred_at']) for m in sent)
        item=by_contact.setdefault(lead['contact_id'],{'first':first,'leads':set()})
        item['first']=min(item['first'],first);item['leads'].add(lead['id'])
    cohort={cid:v for cid,v in by_contact.items() if (not lo or v['first']>=lo) and (not hi or v['first']<hi)}
    ids={lid for v in cohort.values() for lid in v['leads']}
    contact_for={l['id']:l['contact_id'] for l in leads}
    unique=lambda rows:len({contact_for[r['lead_id']] for r in rows if r['lead_id'] in ids})
    bounced={contact_for[m['lead_id']] for m in messages if m['lead_id'] in ids and m['intent']=='BOUNCE'}
    human=[m for m in messages if m['direction']=='IN' and m['intent'] in ('HUMAN','POSITIVE','QUESTION','REJECTED','OPT_OUT')]
    meetings=db.rows('SELECT * FROM meetings');deals=db.rows('SELECT * FROM deals')
    contacted=len(cohort);valid=contacted-len(bounced);replies=unique(human)
    booked=unique([m for m in meetings if m['status'] in ('BOOKED','COMPLETED')]);completed=unique([m for m in meetings if m['status']=='COMPLETED'])
    # Without a date filter show all real bookings too, including manually entered unsent contacts.
    if not lo and not hi:
        allowed={l['id'] for l in leads}
        booked=len({contact_for[m['lead_id']] for m in meetings if m['lead_id'] in allowed and m['status'] in ('BOOKED','COMPLETED')})
        completed=len({contact_for[m['lead_id']] for m in meetings if m['lead_id'] in allowed and m['status']=='COMPLETED'})
    percent=lambda n,d:round(n/d*100,2) if d else None
    return {'scope':f"캠페인 {campaign_id or '전체'} / 업종 {industry or '전체'} / 고유 Contact ID",'date_basis':'최초 실제 발송일 코호트 · Asia/Seoul',
        'from':from_date,'to':to_date,'contacted':contacted,'valid_email':valid,'replied':replies,'positive':unique([m for m in human if m['intent']=='POSITIVE']),
        'booked':booked,'completed':completed,'contracted':unique([d for d in deals if d['stage'] in ('CONTRACTED','PAID')]),
        'revenue':sum(d['amount_krw'] for d in deals if d['lead_id'] in ids and d['stage']=='PAID'),
        'conversion':{'valid_to_reply':{'numerator':replies,'denominator':valid,'percent':percent(replies,valid)},
        'reply_to_booked':{'numerator':booked,'denominator':replies,'percent':percent(booked,replies)},
        'booked_to_completed':{'numerator':completed,'denominator':booked,'percent':percent(completed,booked)}},
        'contacts':len({l['contact_id'] for l in leads}),'leads':len(leads),
        'drafts':len([d for d in db.rows('SELECT lead_id FROM drafts') if d['lead_id'] in {l['id'] for l in leads}]),
        'approved':len([d for d in db.rows("SELECT lead_id FROM drafts WHERE status='APPROVED'") if d['lead_id'] in {l['id'] for l in leads}]),
        'qa_hold':len([d for d in db.rows("SELECT lead_id FROM drafts WHERE status='QA_HOLD'") if d['lead_id'] in {l['id'] for l in leads}]),'send_enabled':False}


def documents(db,campaign_id=None,demo=False):
    links=db.rows('SELECT d.*,l.campaign_id,p.version,p.qa_status FROM document_links d JOIN leads l ON l.id=d.lead_id JOIN proposals p ON p.id=d.proposal_id')
    result=[]
    for link in links:
        if campaign_id is not None and link['campaign_id']!=campaign_id:continue
        events=db.rows('SELECT * FROM document_events WHERE link_id=? AND is_mock=?',(link['id'],int(demo)))
        opens=[e for e in events if e['event_type']=='document_open']
        observed=bool(events)
        result.append({'link_id':link['id'],'lead_id':link['lead_id'],'proposal_id':link['proposal_id'],'proposal_version':link['version'],
            'provider':link['provider'],'url':link['url'],'view_state':'observed' if observed else 'unknown',
            'unique_visits':len({e['session_id'] for e in opens if e['session_id']}) if opens else None,
            'page_views':sum(e['event_type']=='page_visible' for e in events) if events else None,
            'dwell_ms':sum(e['dwell_ms'] or 0 for e in events) if events else None,
            'completion':'unknown','is_mock':demo,'sources':sorted({e['source'] for e in events})})
    return {'scope':'DEMO ONLY' if demo else '실제 관측 이벤트만 · 전체 기간','is_mock':demo,'links':result,
            'identity_notice':'링크 방문은 수신자 신원이나 구매 의사를 확정하지 않습니다.'}
