# Kakeibo

[English](README.md) | **한국어**

일본 거주자를 위한 로컬 가계부 웹앱입니다. Flask와 SQLite로 만들었고, 엔화와 원화 잔액을 따로 관리합니다. Gemini가 영수증과 주문 메일을 거래 내역으로 정리하면, 확인한 뒤 저장할 수 있습니다.

## 주요 기능

- **영수증 읽기:** 사진을 올리면 Gemini가 품목, 금액, 날짜를 추출합니다.
- **Gmail 동기화:** 업체별 코드 없이 주문·배송·영수증 메일을 읽고, 합계가 맞도록 배송비, 할인, 세금을 나눕니다.
- **저장 전 검토:** AI 결과를 검토 화면에서 확인하고, 메일 원문과 비교할 수 있습니다.
- **PayPay와 PASMO:** PayPay CSV를 가져오거나 모바일 PASMO 이용 내역 스크린샷을 올릴 수 있습니다.
- **자동화:** PayPay 가져오기용 분류 규칙과 매월 반복 거래를 지원합니다.
- **예산과 통계:** 항목별 예산, 월별·12개월 차트, Excel 내보내기를 제공합니다.
- **로컬 데이터:** 장부와 비밀 정보는 프로젝트 폴더가 아니라 사용자 폴더에 저장됩니다.

## 빠른 시작

Windows와 Python 3.10 이상이 필요합니다. 영수증과 메일을 읽으려면 [Gemini API 키](https://aistudio.google.com/app/apikey)도 필요합니다.

```bash
git clone https://github.com/phy-ce/kakeibo.git
cd kakeibo
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
run.bat
```

http://localhost:5000 을 엽니다. 처음 실행하면 콘솔에서 `GEMINI_API_KEY`를 묻고 `~/.kakeibo/.env`에 저장합니다.

| | 한국어 | 영어 |
|---|---|---|
| 실행 파일 | `run.bat` | `run_en.bat` |
| 포트 | 5000 | 5001 |
| 데이터 폴더 | `~/.kakeibo` | `~/.kakeibo-en` |

두 인스턴스는 동시에 실행할 수 있고 데이터를 공유하지 않습니다. 다른 데이터 폴더를 쓰려면 `KAKEIBO_HOME`을 설정하세요.

## 선택 설정

아래 경로는 한국어 인스턴스 기준입니다. 영어 인스턴스는 `~/.kakeibo-en`을 사용하세요.

- **Gmail 동기화:** [Google Cloud Console](https://console.cloud.google.com/)에서 Gmail API를 켜고 데스크톱 OAuth 클라이언트를 만듭니다. 설정 화면에서 `credentials.json`을 올린 뒤, 첫 동기화 때 브라우저에서 접근을 승인합니다.
- **분류 규칙:** `auto_rules.json.example`을 `~/.kakeibo/auto_rules.json`으로 복사해 수정합니다.
- **초기 카테고리:** 첫 실행 전에 `categories.json.example`을 `~/.kakeibo/categories.json`으로 복사합니다. 이후에는 설정 화면에서 관리합니다.

## 프로젝트 구조

```text
kakeibo/
├── app.py          Flask 라우트
├── db.py           SQLite 스키마와 마이그레이션
├── ai_items.py     AI 추출 항목 정리
├── auto_rules.py   분류 규칙
├── recurring.py    매월 반복 거래
├── exchange.py     환율
├── i18n.py         한국어·영어 UI 문구
├── ocr/            Gemini 영수증 읽기
├── sync/           Gmail 인증과 메일 해석
├── templates/      Jinja2 HTML
└── static/         CSS와 JavaScript
```

## 라이선스

MIT. [LICENSE](LICENSE)를 참고하세요.
