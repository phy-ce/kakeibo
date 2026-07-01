# CLAUDE.md

일본 거주자용 개인 가계부 (Flask + SQLite 로컬 웹앱). 엔(¥)/원(₩) 잔고를 독립 관리하고, 영수증 OCR·메일 파싱은 Google Gemini 를 쓴다.

## 실행 / 빌드

```bash
run.bat                # python -m kakeibo (브라우저 자동 오픈) → http://localhost:5000
build.bat              # PyInstaller 로 dist/kakeibo.exe 생성 (선택)
pip install -r requirements.txt
```

- 진입점: `kakeibo/__main__.py` → `main()`. 앱 import **전에** `config.prompt_missing_keys()` 로 시크릿 준비(순서 중요 — app/ocr 모듈이 import 시 env 를 읽음).
- 포트 변경: env `FLASK_PORT`. 브라우저 자동오픈 끄기: `KAKEIBO_NO_BROWSER=1`.

## 핵심 원칙: 코드와 데이터 분리

**프로젝트 폴더에는 코드(엔진)만.** 모든 실제 데이터·시크릿은 사용자 홈 **`~/.kakeibo/`** (Windows `%USERPROFILE%\.kakeibo\`)에 저장된다. 위치는 env `KAKEIBO_HOME` 으로 재정의 가능.

`~/.kakeibo/` 안: `kakeibo.db`, `.env`(GEMINI_API_KEY, FLASK_SECRET_KEY, 선택 GOOGLE_CLIENT_ID/SECRET), `credentials.json`/`token.json`(Gmail), `uploads/receipts/`(영수증 이미지), `temp/`(처리중 임시), `categories.json`/`auto_rules.json`(선택).

- 경로 헬퍼: `config.user_config_dir()`, `config.user_path(name)`, `config.resolve_secret_file(name)`(사용자 홈 우선, 없으면 프로젝트 루트 폴백).
- `config.migrate_project_data()` — 옛 버전이 프로젝트 폴더에 만든 데이터를 `~/.kakeibo` 로 1회 자동 이전(`init_db()` 시작 시 호출).
- `paths.app_dir()` 는 이제 레거시 폴백/이전용으로만 쓴다(데이터 기본 위치 아님).

## 구조

```
kakeibo/                     # 파이썬 패키지
├── __main__.py              # 진입점
├── app.py                   # Flask 라우트 전체 (페이지 + /api/*)
├── config.py                # 시크릿/데이터 경로, .env 로드, Gmail 상태, 데이터 이전
├── db.py                    # SQLite 스키마/마이그레이션, 데이터 경로, 백필
├── exchange.py              # 환율 조회
├── auto_rules.py            # 자동 분류 룰 (PayPay 임포트에만 적용)
├── ocr/gemini.py            # Gemini: 영수증 OCR(analyze_receipt) + 범용 JSON(generate_json), QuotaError
├── sync/gmail_auth.py       # Gmail OAuth (token.json 우선, 없으면 credentials.json 으로 인증)
├── sync/generic.py          # 범용 메일 → 거래 (AI 파싱, 벤더 무관)
└── templates/, static/
```

## 데이터 모델 (transactions 테이블)

한 거래(품목) = 한 행. `source` 값: `manual`/`paypay`/`receipt`/`email`. 소스별 중복방지 컬럼:
- `paypay_id`, `gmail_id`(범용 메일), `receipt_name`(영수증 원본 파일명), `amazon_id`/`mcdonald_id`(레거시)
- `receipt_id` — **영수증 한 장에 속한 품목들을 묶는 id**. 영수증 confirm 시 사진 1장당 1개 부여, 모든 품목이 공유. `receipt_img` 도 그 그룹의 모든 행에 연결됨.
- 잔고(jpy_balance/krw_balance)는 조회 시 `get_transactions_list()` 가 초기잔고+누적으로 계산(컬럼 아님).

## 영수증 입력 흐름 (3단계 API)

1. `POST /api/receipts/stage` — 사진 저장(`~/.kakeibo/temp/{sid}/`), **파일명으로 중복 표시**. OCR 안 함.
2. 확인 화면(`receipt_stage.html`)에서 분석할 사진 선택 → `POST /api/receipts/analyze` — 선택분만 `compress_image`(긴변 1080/q65) 후 Gemini OCR. 할당량 초과 시 중단.
3. 리뷰(`receipt_review.html`) 편집 → `POST /api/receipts/confirm` — 품목별 행 INSERT, `receipt_id`(사진당) + `receipt_img`(그룹 전체) 부여, 이미지를 `uploads/receipts/` 로 이동.

거래 목록(`transactions.html`)은 `_receipt_units()` 가 `receipt_id`(폴백: 날짜+가게)로 묶어 **접기/펼치기 그룹**으로 표시. 이미지는 라이트박스(base.html의 `showReceipt`)로 클릭 확대.

## 범용 메일 동기화 (sync/generic.py)

넓은 Gmail 쿼리로 가져와 **AI 호출 전 사전필터**(`_looks_like_purchase`)로 구매 메일만 골라 Gemini 로 파싱. 중복방지 `gmail_id`. `/api/email/sync`.

- 필터: 금액 패턴 없으면 스킵 → 화이트리스트 발신자 or 구매 키워드면 통과.
- 사용자 설정(설정 페이지): `email_sender_allowlist`, `email_keywords`(쉼표구분, **채우면 기본 대체·비우면 기본값**), `mail_sync_days`. 기본값은 `generic._DEFAULT_SENDERS/_DEFAULT_KEYWORDS`.
- 무료 할당량(gemini-3.5-flash: 분당 5·일 20건) 대응: 429 시 `gemini.QuotaError` → 동기화 **즉시 중단**(홍수 방지), 처리분 저장, 다음 실행에 이어서.

## 주의사항

- **Windows 한국어 콘솔(cp949)**: 콘솔 출력에 `—`/`→`/이모지 등 비-cp949 문자를 쓰면 `UnicodeEncodeError`. 콘솔 print 는 ASCII 로.
- 파일 저장은 항상 `encoding="utf-8"`.
- `auto_rules` 는 현재 PayPay 임포트에만 적용됨(영수증/메일 아님).
- 설정 저장은 `POST /api/settings` 가 받은 key 를 그대로 `settings` 테이블에 넣음(임의 키 가능).
- 첫 실행 온보딩: `setup_done` 미설정이면 잔고 입력 모달, Gmail 미연동이면 알림 배너(`gmail_reminder_dismissed` 로 숨김).

## 작성 규칙 (Conventions)

- **Commit messages must always be written in English.**
- **Code comments and docstrings must be written in English.** User-facing strings — UI/template text, `print()`/console output, error/`jsonify` messages, and Gemini prompt strings — stay Korean.
