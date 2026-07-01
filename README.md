# Kakeibo

일본 거주자용 개인 가계부 — Flask + SQLite 기반 로컬 웹앱.
엔(¥) / 원(₩) 잔고를 **독립적으로** 관리하며, 환율은 참고용으로만 표시.

## 주요 기능

- 📷 **영수증 OCR**: 사진 업로드하면 Gemini Flash 가 품목/금액 자동 추출
- 💳 **PayPay CSV 임포트**: PayPay 거래내역 CSV 파일 한 방에 임포트
- 📧 **Gmail 메일 동기화 (AI)**: 벤더 상관없이 주문/발송/영수증 메일을 Gemini 가 읽어 품목·금액·날짜 자동 추출 (Amazon·맥도날드·BIRKENSTOCK 등 전부, 벤더별 코드 불필요). 배송비·할인·소비세도 분해해 합계 일치
- 🤖 **자동 분류 룰**: 구입처/품목/카테고리 매칭으로 카테고리/품목명 자동 채움 (`auto_rules.json`)
- 💰 **개별 항목 예산**: 카테고리 묶음이 아닌 개별 거래 항목 단위 예산 (넷플릭스, 가스비 등)
- 📊 **통계 / 휴지통 / Excel 내보내기**: Chart.js 그래프, 소프트 삭제, openpyxl 엑셀

## 설치 (소스)

요구사항: Python 3.10+

```bash
git clone https://github.com/<YOUR_GITHUB_USERNAME>/kakeibo.git
cd kakeibo

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
```

### 시크릿 설정

시크릿(API 키)은 **프로젝트 폴더가 아니라 사용자 홈**(`~/.kakeibo/.env`, Windows 는 `%USERPROFILE%\.kakeibo\.env`)에 저장됩니다. 별도 설정 없이 **첫 실행 때 콘솔이 자동으로 안내**합니다:

- `FLASK_SECRET_KEY` — 자동 생성·저장 (신경 쓸 필요 없음)
- `GEMINI_API_KEY` — 없으면 첫 실행 시 콘솔이 입력을 요구 → 입력하면 `~/.kakeibo/.env` 에 저장. 이후 실행부터는 다시 묻지 않음
  - 키 발급(무료): [Google AI Studio](https://aistudio.google.com/app/apikey)
  - 나중에 바꾸려면 `~/.kakeibo/.env` 를 직접 편집
  - 저장 위치를 바꾸려면 환경변수 `KAKEIBO_HOME` 지정

> 프로젝트 루트에 `.env` 를 두면 그 값도 읽지만(하위호환), 사용자 홈 `.env` 가 우선합니다.

2. **(선택) Gmail 동기화** — 주문/영수증 메일 가져오기 쓸 경우:
   - [Google Cloud Console](https://console.cloud.google.com/) → 새 프로젝트 → Gmail API 활성화 → OAuth 클라이언트 (데스크톱) 생성
   - 다운로드한 `credentials.json` 을 **설정 페이지 → Gmail 연동 → 업로드** 로 올리면 끝 (`~/.kakeibo/credentials.json` 에 저장)
   - 첫 동기화 시 브라우저에서 권한 동의 → `~/.kakeibo/token.json` 자동 생성 (이후 재사용)

3. **(선택) 자동 분류 룰**:
   - `auto_rules.json.example` 을 **`~/.kakeibo/auto_rules.json`** 으로 복사 후 본인 룰로 편집

4. **(선택) 초기 카테고리 커스터마이즈**:
   - `categories.json.example` 을 **`~/.kakeibo/categories.json`** 으로 복사 후 본인 카테고리로 편집
   - **첫 실행 전에만 적용됨** (DB가 비었을 때만 시드). 이미 DB가 있으면 설정 페이지에서 추가/삭제하면 됨

> **데이터/코드 분리**: 프로젝트 폴더에는 **코드(엔진)만** 둡니다. 실제 데이터 — DB(`kakeibo.db`), 업로드/임시 영수증, 시크릿(`.env`, `credentials.json`, `token.json`), `auto_rules.json`, `categories.json` — 는 전부 **`~/.kakeibo/`** (Windows `%USERPROFILE%\.kakeibo\`) 에 저장됩니다. 저장 위치는 `KAKEIBO_HOME` 환경변수로 변경 가능. (이전 버전이 프로젝트 폴더에 만든 데이터는 첫 실행 시 자동으로 옮겨집니다.)

### 실행

```bash
run.bat                          # Windows (브라우저 자동 오픈)
# python -m kakeibo              # 직접 실행
```

→ http://localhost:5000

## 설치 (Windows exe)

[Releases](../../releases) 에서 `kakeibo-*.zip` 다운로드:

1. 압축 풀기 (아무 폴더에)
2. `kakeibo.exe` 더블클릭 → 콘솔 창이 뜨며 **첫 실행 시 `GEMINI_API_KEY` 입력을 안내** (키는 `~/.kakeibo/.env` 에 저장, 다음부터 안 물음)
3. (선택) Gmail 동기화 쓰면 `credentials.json` 을 exe 같은 폴더에 두기
4. 이후 브라우저 자동 오픈

DB(`kakeibo.db`), 업로드 폴더, 임시 폴더는 exe 옆에 자동 생성됨. API 키만 사용자 홈(`~/.kakeibo`)에 별도 보관.

## 폴더 구조

```
kakeibo/                    # repo 루트
├── kakeibo/                # Python 패키지
│   ├── app.py              # Flask 라우트
│   ├── db.py               # SQLite 스키마/마이그레이션
│   ├── exchange.py         # Yahoo Finance 환율
│   ├── auto_rules.py       # 자동 분류 룰 엔진
│   ├── paths.py            # 소스/exe 양쪽 호환 경로 헬퍼
│   ├── sync/               # Gmail 기반 동기화
│   │   ├── gmail_auth.py   # 공통 OAuth
│   │   └── generic.py      # 범용 메일 파싱 (AI, 벤더 무관)
│   ├── ocr/
│   │   └── gemini.py       # 영수증 OCR
│   ├── templates/          # Jinja2 HTML
│   └── static/             # CSS/JS
├── requirements.txt
├── run.bat / build.bat
├── .env.example
└── auto_rules.json.example
```

## 라이선스

MIT — `LICENSE` 참조.
