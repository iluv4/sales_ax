# 데이터 모델 — 사람, 업무, 실제 사건을 분리

| 구분 | 테이블 | 의미 |
|---|---|---|
| 고객 정의 | persona_sessions / persona_messages / persona_versions | 대화 원문·수정한 카드·승인 버전 |
| 영업 설정 | campaigns / offers / icp_profiles | 누구에게 무엇을 제안하는가 |
| 사람·조직 | organizations / contacts / leads | 회사, 고유 사람, 캠페인별 영업 관계 |
| 조사 | evidence / source_records | 출처·확인 시각·원본 가져오기 근거 |
| 문서 | templates / proposals / document_links / document_events | 원본·개인화 자료·수신자별 링크·관측 사건 |
| 메일 | drafts / messages / send_attempts | 미발송 문서, 실제 메시지, 발송 시도의 구분 |
| 미팅 | meetings / meeting_notes / tasks | 예약과 실제 완료, 기록, 후속 작업 |
| 성과 | deals | 사람이 확인한 계약·매출 |
| 운영 | jobs / sync_runs / integration_health / audit_logs | 작업·동기화·연결 확인·변경 이력 |

Contact ID는 사람의 번호, Lead ID는 ‘그 사람에게 특정 캠페인으로 제안하는 업무’의 번호다. 같은 사람이 두 캠페인에 있어도 사람 수는 1명이다. 같은 회사의 다른 사람은 합치지 않는다.

중복 방지: 정규화 이메일, campaign+contact, lead+sequence, Gmail account+message ID, Calendar external ID, provider+external event ID, 작업 idempotency key가 고유하다. 출처에 Gmail 스레드 ID가 없으면 만들지 않는다.

`BOOKED`는 예약이고 `COMPLETED`는 실제 참석을 확인한 상태다. `DRAFT_CREATED`는 발송이 아니다. `unknown`은 열람 데이터가 없다는 뜻이며 미열람으로 단정하지 않는다. 가상 문서 이벤트는 is_mock=true로 분리한다.

SQLite 추가 변경은 migrations.py가 기존 데이터를 지우지 않고 적용한다. 운영 PostgreSQL 전환 시 같은 제약을 DB 마이그레이션으로 옮긴다. 파일과 비공개 DB는 GitHub에 올리지 않는다.
