# Sales AX MVP

모범AI Sales AX의 로컬 단일 사용자 개발 버전입니다. Codex에서 구현하며 `iluv4/sales`와 `iluv4/sales_saas_prompt`를 읽기 참조로 보존합니다.

## 실행

Python 3.11 이상 환경에서:

```sh
python -m pip install -r requirements.txt
python run.py
```

웹: http://127.0.0.1:8765/ · API 문서: http://127.0.0.1:8765/docs

```sh
python checks.py
```

현재 검증(2026-11-02): 원본 sales 28개, sales_saas_prompt 16개, 통합 36개 — **80개 통과**. 모든 발송 테스트는 가짜 Gmail 클라이언트를 사용합니다.

## 2026-11-02 추가분 (Claude Code)

| 기능 | 위치 | 상태 |
|---|---|---|
| 기존 통합 시트 읽기 전용 가져오기: 탭 CSV 업로드 → 미리보기 → 확정. 같은 리드ID는 다시 만들지 않음 | `ax/sheet_import.py`, `POST /api/imports/sheet-snapshot`, 고객 발굴 화면 `기존 시트 가져오기` | 가상 데이터 테스트 통과 · 실제 9명 시트는 아직 가져오지 않음 |
| 고객 통합 타임라인: 근거·제안서·초안·발신/수신·문서 링크·미팅 예약/실제 완료·계약을 출처·실행자·검증 정도와 함께 시간순 표시 | `ax/timeline.py`, `GET /api/leads/{id}/timeline`, 고객 360° 화면 | 동작 |
| 퍼널 필터: `campaign_id`, `industry`, `start`, `end`(최초 실제 발송일 코호트) | `GET /api/analytics/funnel` | API 동작 · 화면 필터는 미연결 |

시트 가져오기 상태는 `MANUAL`로 표시합니다(파일 가져오기이지 Google API 실시간 연결이 아님). 시트의 발송 기록에는 Gmail 스레드 ID가 없으므로 타임라인에 `sent_record_only`로 표시되고, `기존 발신 메일 연결`(Gmail OAuth 후)로 스레드를 채웁니다.

## 현재 동작 범위

- 캠페인·ICP, 연락처와 캠페인별 리드, 출처 근거, 연락처 검증과 ICP 평가
- 이메일 정규화·중복 제거, 수신거부의 캠페인 간 적용
- 로컬 초안, 버전별 QA·승인, PDF 업로드와 고객 매핑
- Gmail API 초안 생성·업데이트, 본문·수신자·첨부 해시 재읽기 검사
- 스레드 및 RFC Message-ID 기반 회신 매칭, 후속 작업
- 미팅 예약과 명시적 실제 완료 분리, 수동 계약·매출 보존
- 대시보드·기본 퍼널, 감사 로그, PPTX 원본·변수맵 등록

**실제 발송 API는 비활성화되어 있습니다.** QA와 승인은 발송을 실행하지 않습니다.

## 연결 상태를 구분하세요

2026-11-02 확인 기준:

| 경로 | 상태 |
|---|---|
| Codex Gmail 커넥터 | 회사 계정 확인·실제 SENT 메시지/스레드 읽기 성공 |
| Codex Calendar 커넥터 | 회사 계정·캘린더 목록·제한 기간 일정 읽기 성공 |
| Sales AX 로컬 Gmail OAuth | 미설정; 실제 초안 생성은 아직 라이브 검증 전 |
| Sales AX 로컬 Calendar OAuth | 미설정; 개발 중 어댑터의 API/UI 연결 미완료 |
| 원본 Sheets | 커넥터 읽기 성공; 9명 배치 확인, 로컬 반영 미완료 |
| 원본 PPTX/PDF 자동 생성 | 템플릿·변환 엔진 미연결 |
| FeatPaper·내부 PDF 추적 | 개발 중 모듈 존재; 현재 API/UI에 미연결 |

`ax/documents.py`, `ax/analytics.py`, `ax/google_readonly.py`, `ax/sheet_import.py`에는 진행 중 코드가 있습니다. 파일 존재를 완료·실제 연동 성공으로 해석하지 마세요. 현재 동작의 기준은 `ax/api.py`의 연결된 엔드포인트와 통합 테스트입니다.

## 자격증명

Gmail은 저장소 밖에 보관한 authorized-user JSON 경로를 `GMAIL_AUTHORIZED_USER_FILE`로 지정하거나 적절한 ADC 환경을 사용합니다. 필요한 권한은 `gmail.compose`, `gmail.readonly`입니다. 실제 회사 OAuth 앱 설정·동의·계정 확인이 별도로 필요합니다. Codex 커넥터의 OAuth 토큰은 로컬 앱에 전달되지 않습니다.

토큰·비밀키·실제 고객 DB·내보낸 메일·캘린더 상세는 커밋하지 않습니다. 이 저장소에는 실제 고객 데이터가 포함되지 않습니다. 테스트의 연락처는 가상 예시입니다.

## UI 촬영

[UI_REVIEW.md](UI_REVIEW.md)에 실제 8개 화면과 입력 모달의 데스크톱·모바일 캡처 19장, 구현/미구현 구분, 화면별 경로와 문제점이 있습니다. 촬영 당시 고객·초안 데이터는 비어 있었습니다. 디자인 작업은 현재 보류하고 연동 기능을 우선합니다.

## 원본 보존

- `vendor/sales`: `670448c013ecfdd0629dafbcf9a2cc1c05398e7f`
- `vendor/saas`: `674375f069aba1c00a16678afbef6b473a989dee`

원본 두 저장소를 변경하거나 DB 스키마를 그대로 합치지 않았습니다. 공통 모델·안전한 어댑터를 별도로 구현했습니다.

## 다음 구현

1. 로컬 OAuth 연결과 실제 Gmail 미발송 초안·PDF 읽기 검증
2. Calendar 읽기 전용 동기화 API 연결 및 완료 상태 보존 테스트
3. 9명 원본 배치의 재가져오기 멱등성 검증
4. 문서 수동 링크·unknown/실제/데모 분리 API와 화면 연결
5. 제공된 원본 PPTX 기반 렌더링과 PDF 변환 품질 검증

현재는 로컬 개발 버전입니다. 외부 공개·다중 사용자 인증·운영 배포는 아직 구성하지 않았습니다.
