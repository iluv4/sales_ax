"""One customer timeline built from the source tables. Each item keeps its origin, actor and verification level.

Nothing here infers events: a draft is not a send, a booking is not a completed meeting, a mock view stays mock.
"""
from datetime import datetime,timezone


def item(at,kind,title,detail='',source='local',actor='system',verification='recorded',is_mock=False):
    return {'at':at,'kind':kind,'title':title,'detail':detail,'source':source,'actor':actor,'verification':verification,'is_mock':is_mock}


def key(entry):
    try:
        stamp=datetime.fromisoformat(entry['at'])
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError):return datetime.min.replace(tzinfo=timezone.utc)


def lead_timeline(wf,lid):
    db=wf.db;lead=wf.lead(lid);events=[]
    for s in db.rows('SELECT provider,external_id,imported_at FROM source_records WHERE lead_id=?',(lid,)):
        events.append(item(s['imported_at'],'imported','원본 자료에서 가져옴',s['provider'],s['provider'],'import','source_only'))
    for e in db.rows('SELECT * FROM evidence WHERE contact_id=?',(lead['contact_id'],)):
        events.append(item(e['observed_at'],'evidence',e['claim'],e['source_url'],'public_source','operator','human_checked' if e['human_checked'] else 'unverified'))
    for p in db.rows('SELECT id,filename,qa_status,identity_checked,source_url,created_at FROM proposals WHERE lead_id=?',(lid,)):
        verification='source_only' if p['qa_status']=='SOURCE_ONLY' else ('human_checked' if p['identity_checked'] else 'unverified')
        events.append(item(p['created_at'],'proposal',p['filename'],p['source_url'] or p['qa_status'],'google_sheet' if p['source_url'] else 'upload','operator',verification))
    for d in db.rows('SELECT * FROM drafts WHERE lead_id=?',(lid,)):
        detail=f"v{d['version']} · {d['status']} · Gmail {d['sync_state']}"+(f" · 승인 {d['approved_by']}" if d['approved_version'] else '')
        events.append(item(d['created_at'],'draft',d['subject'],detail+' · 초안은 발송이 아닙니다','local','operator','gmail_verified' if d['sync_state']=='VERIFIED' else 'local_only'))
    for m in db.rows('SELECT * FROM messages WHERE lead_id=?',(lid,)):
        outgoing=m['direction']=='OUT'
        detail=('스레드 미확인 · ' if not m['thread_id'] else '')+(m['intent'] or '')+(' · 수신 도달은 확정 아님' if outgoing else '')
        events.append(item(m['occurred_at'],'email_sent' if outgoing else 'email_received',m['subject'] or '(제목 없음)',detail.strip(' ·'),m['source'],
            m['classified_by'] or ('gmail' if m['source']=='gmail_api' else 'import'),'thread_matched' if m['thread_id'] else 'sent_record_only',bool(m['is_mock'])))
    for link in db.rows('SELECT * FROM document_links WHERE lead_id=?',(lid,)):
        events.append(item(link['created_at'],'document_link','수신자별 문서 링크',link['url'],link['provider'],'operator',link['verification_state']))
        for ev in db.rows('SELECT * FROM document_events WHERE link_id=?',(link['id'],)):
            events.append(item(ev['occurred_at'],'document_event',ev['event_type'],ev['observation_source'] or '',ev['source'],'observer','mock' if ev['is_mock'] else 'observed',bool(ev['is_mock'])))
    for m in db.rows('SELECT * FROM meetings WHERE lead_id=?',(lid,)):
        events.append(item(m['starts_at'],'meeting_booked','미팅 예약 · '+m['status'],'캘린더 '+m['external_id'] if m['external_id'] else '수동 예약 기록',
            'calendar' if m['external_id'] else 'manual','calendar' if m['external_id'] else 'operator','booked'))
        if m['status']=='COMPLETED':
            events.append(item(m['completed_at'],'meeting_completed','실제 미팅 완료',m['notes'],'manual',m['confirmed_by'],'human_checked'))
    deal=db.one('SELECT * FROM deals WHERE lead_id=?',(lid,))
    if deal:events.append(item(deal['verified_at'],'deal',f"{deal['stage']} · {deal['amount_krw']:,}원",deal['evidence'],'manual',deal['confirmed_by'],'human_checked'))
    events.sort(key=key,reverse=True)
    return events
