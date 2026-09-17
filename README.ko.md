# SourceLedger

[English](README.md) | [한국어](README.ko.md)

![SourceLedger — Collect information. Preserve its source.](SourceLedger.png)

SourceLedger는 출처를 탐색하고 상품 가격과 원문·근거를 보존하며 XLSX 보고서를 만드는 로컬 도구입니다. 가격을 추정하거나 빈 값을 0으로 채우지 않습니다.

수집 결과에는 원본 URL, 수집 시각(UTC), 원문 필드, CSS/JSON/표 위치, 증거 파일 경로와 SHA-256을 남깁니다. 상품·가격·통화가 원문 근거와 함께 확인된 행은 `verified`가 될 수 있습니다. 비교 조건이 부족하면 `verified`여도 `comparable=false`이며 가격 비교에는 넣지 않습니다. 특히 묶음(pack)인지 개별(each)인지 원문 `price_basis` 근거가 없으면 정규화 단가를 계산하지 않고 빈칸으로 남깁니다.

## 빠른 시작

Python 3.11 이상 환경에서 설치합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test,mcp]"
```

브라우저 수집을 쓰려면 선택 의존성과 Chromium을 별도로 설치합니다.

```powershell
pip install -e ".[browser]"
python -m playwright install chromium
```

데모는 합성 HTML/CSV만 읽으며 실제 시장 가격을 사용하지 않습니다.

```powershell
source-ledger run --config examples/demo.json
source-ledger status --config examples/demo.json --run-id <실행_ID>
source-ledger observations --config examples/demo.json --run-id <실행_ID>
source-ledger export --config examples/demo.json --run-id <실행_ID>
source-ledger doctor
```

`su-crawler`는 호환을 위해 유지되는 명령 별칭입니다. 새 스크립트와 문서에는 `source-ledger`를 사용하세요.

`run --max-tasks N`으로 감독 가능한 배치만 실행할 수 있고, 반환된 실행 ID로 `run --resume <실행_ID>`를 재개합니다. 산출물은 설정의 `output_dir` 아래 SQLite 실행 이력, 원문 증거, XLSX 보고서로 저장됩니다.

## 설정 범위

처음에는 연구 워크스페이스를 만듭니다. 기본 언어는 영어이며 `--lang ko`로 한국어 설정 질문과 다음 작업 안내를 선택할 수 있습니다.

```powershell
source-ledger init --lang ko
source-ledger research-status
```

산업군·상품·대상 시장만 입력받고 분석 목적은 묻지 않습니다. 이후 출처 URL 등록, 링크 후보 탐색, 정확한 식별자 입력, 수집 초안 생성으로 이어집니다. 전체 명령은 [시작 안내](docs/GETTING_STARTED.ko.md), 남은 자동화 범위는 [로드맵](docs/ROADMAP.ko.md)에 정리했습니다.

연구 워크스페이스는 상품 주제 하나를, 수집 설정은 여러 개의 정확한 상품을 지원합니다. `draft`는 수동 설정의 시작점이며, `propose`는 원문 HTML을 검사해 추출 규칙을 제안합니다. 둘 다 확인된 관측과 구분됩니다.

### 범위가 정해진 연구 실행

0.3에는 선택적 SearXNG 키워드 검색, 원문 기반 선택자 제안, 진행 저장·재개가 가능한 실행 제어기를 추가했습니다. 초기 설정 후 출처와 식별자를 등록하고 실행합니다.

```powershell
source-ledger product-set --identifier model=YOUR_EXACT_MODEL
source-ledger source-add --url "https://your-vendor.example/product"
source-ledger agent --run-dir .sourceledger/runs/run-001 --max-sources 3 --max-seconds 120
```

선정한 출처의 규칙을 제안한 뒤 전체 출처를 새로 수집·검증해 SQLite와 XLSX에 저장합니다. 불완전한 출처도 결과에 남습니다. `--resume`은 저장한 범위와 한도를 유지하며, `--max-steps`로 출처 처리 사이에서 일시 정지할 수 있습니다. 개별 외부 요청에 타임아웃을 적용하고 전체 실행 시간을 제한하지만 프로세스를 강제로 종료하는 방식은 아닙니다.

키워드 검색에는 직접 설정한 SearXNG 서버가 필요합니다. Ollama 보조는 클라우드 기능을 끈 로컬 서버와 설치된 모델을 명시해야 하며 모델 호출 기본 한도는 0회입니다. 모델은 선택자만 제안합니다. 자동 활성화에는 `--activate`와 모든 선택 출처를 포함하는 정답 표본이 추가로 필요합니다. 설정과 한계는 [자동화 안내](docs/AUTOMATION.ko.md)를 참고하세요.

설정은 JSON이며 제품마다 원문과 대조할 `identifiers`를, 출처마다 고정 `location`과 `product_ids`를 둡니다. 웹 출처는 `allowed_domains`에 URL 호스트를 명시해야 합니다. 내부 자료는 로컬 file 출처의 `file_root` 또는 권한 있는 internal 웹 출처로 제한해 설정합니다. file 출처는 기준 디렉터리 또는 명시된 `file_root` 안의 파일만 읽습니다.

```json
{
  "name": "부품 가격 수집",
  "output_dir": "outputs",
  "products": [{"id":"part-a", "name":"부품 A", "identifiers":{"model":"A-100"}}],
  "sources": [{
    "id":"vendor-a", "name":"공개 카탈로그", "kind":"web",
    "location":"https://example.com/catalog/a-100", "allowed_domains":["example.com"],
    "product_ids":["part-a"], "backends":["http", "playwright"],
    "selectors":{"model":".model", "price":".price", "currency":".currency"}
  }]
}
```

보고서는 실행 요약, 수집 현황, 가격 비교, 관측 이력, 출처 근거, 검토 필요 시트로 구성됩니다. 가격 비교에는 확인·최신·비교 가능 관측만 들어가며, 같은 비교 조건에서 출처별 최신 관측 하나씩으로 최저·최고·중앙값·건수를 계산합니다. `price_basis`(예: `pack`, `each`)와 포장 수량은 독립된 비교 조건이며 원문 근거가 없으면 비교·정규화하지 않습니다. 보고서는 월별 가격 이력이 아니라 하나의 실행(run) 단위 관측 보고서입니다.

### 추출 규칙과 브라우저 레시피

HTML은 `selectors`와 선택 `row_selector`의 CSS 위치를 증거로 남깁니다. CSV는 `columns`의 헤더 매핑, XLSX는 `sheet`와 `columns` 매핑, JSON은 배열 또는 `items`/`products` 객체, PDF는 `pdf_pattern`의 named capture 그룹으로 명시적으로 추출합니다. 스캔 PDF의 OCR은 구현하지 않았으며, 텍스트 계층이 없으면 검토 대상으로 남습니다.

`profile_dir`을 설정하면 이미 로그인된 전용 브라우저 프로필을 재사용할 수 있습니다. 이 기능은 로그인 절차나 자격증명 입력을 자동화하지 않습니다. `recipe`에는 명시적인 페이지 동작만 둡니다. 현재 `click`, `select`, `fill`, `wait_for`, `assert_text`를 지원합니다.

```json
"recipe": [
  {"action":"click", "selector":"button[data-tab='prices']"},
  {"action":"select", "selector":"select#pack", "value":"100"},
  {"action":"wait_for", "selector":".price", "state":"visible"},
  {"action":"assert_text", "selector":".currency", "text":"KRW"}
]
```

### URL 후보 발견

`discover`는 설정된 출처의 HTML 링크 또는 XML sitemap에서 지정 개수 이내의 URL 후보를 반환합니다. 설정된 backend를 각각 최대 한 번 사용하고, HTTP 실패나 빈 링크 결과 뒤에는 Playwright도 시도합니다. 정책상 접근 거절은 fallback을 중단합니다. 후보는 같은 `allowed_domains`의 HTTP(S) URL로 제한되며 fragment·중복·인증 정보 URL은 버립니다.

```powershell
source-ledger discover --config examples/demo.json --source-id catalog --limit 100
```

`source-discover`는 발견한 링크를 연구 워크스페이스에 후보로 저장합니다. 관련성을 검토하고 수집 초안에서 의도한 출처만 남긴 뒤 선택자/열 매핑을 작성합니다. 발견만으로 확인된 가격 관측을 만들지는 않습니다.

### 검증과 활성화

아래 예시는 합성 가격 두 개를 정답 표본과 대조하고 검증 기록과 활성 설정을 만듭니다. 외부 사이트에 접근하지 않습니다.

```powershell
source-ledger verify --config examples/verification.json --samples examples/verification.samples.json --receipt .sourceledger/verification.receipt.json
source-ledger activate --config examples/verification.json --receipt .sourceledger/verification.receipt.json --output .sourceledger/active.json
source-ledger run --config .sourceledger/active.json
```

모든 상품에 출처가 연결되고, 모든 출처·상품 작업이 확인 상태이며 최신·비교 가능 관측이 있어야 활성화할 수 있습니다. 설정·근거 원문·추출 필드·가격 유효기한·입력한 정답 표본을 다시 확인합니다. 규칙 변경·근거 누락·검증 만료 시 재검증이 필요합니다. 검증 기록과 활성 설정은 덮어쓰지 않으므로 새 버전에는 새 파일명을 사용합니다.

`--samples`를 생략하면 원문과의 일관성만 검사합니다. 검증 기록은 로컬 감사 기록이며 실제 시장 정확도를 보증하는 서명은 아닙니다. 기존 `run --config`는 수동 관리 설정에도 계속 사용할 수 있습니다. 활성화 절차를 거쳐야만 실행되도록 강제한 구조는 아닙니다.

## MCP 사용

MCP 서버가 수집 worker를 하위 프로세스로 만들지 않습니다. Windows에서 클라이언트의 Job Object가 하위 프로세스까지 종료할 수 있기 때문입니다. **정상 터미널에서 독립 worker를 먼저 실행**한 뒤 MCP를 실행하세요.

```powershell
# 터미널 1: 독립 worker
python -m su_crawler.worker --config examples/demo.json

# 터미널 2: stdio MCP 서버
source-ledger serve-mcp --config examples/demo.json
```

worker의 heartbeat가 확인되지 않으면 MCP는 수집 시작 요청을 거절합니다. MCP 도구는 고정 설정 범위 안에서 작업 등록·상태 조회·관측 조회·XLSX 재생성을 수행합니다. `streamable-http`은 로컬 `127.0.0.1`만 지원합니다.

## 현재 한계

- 키워드 검색은 직접 설정한 SearXNG 서버를 사용합니다. 임의의 공용 검색 서버를 기본으로 호출하지 않습니다.
- 구조화된 데이터·의미가 명시된 HTML의 선택자 제안과 선택적 로컬 모델 보조를 지원합니다. 새 브라우저 동작 계획, 자동 로그인, 무인 규칙 복구는 미구현입니다.
- SearXNG/Ollama 요청 경로는 통제된 응답으로 검사했으며 실제 서버·모델·대표 실사이트 검증은 남아 있습니다. 루프백 주소만으로 모델 실행 위치를 보증하지 않으므로 Ollama 클라우드 기능을 꺼야 합니다.
- Claude/ChatGPT 실제 UI 연결은 검증하지 않았습니다. 원격 ChatGPT 연결에는 인증된 배포와 산출물 다운로드 경로가 필요합니다.
- Crawl4AI는 선택 의존성이고 이 환경에서 설치·실사이트 동작을 검증하지 않았습니다.
- UWS, Jina, Exa 등 비용 또는 외부 전송이 발생하는 서비스에는 연결하지 않습니다.
- Agent-Reach는 doctor/routing 설계 참고 범위이며 직접 실행 연동은 없습니다. ego-lite는 macOS 앱 의존 경로로, 현재 Windows에서 직접 연동하지 않았습니다.
- 독립 worker를 Windows 서비스로 등록하거나 스케줄링해 상시 운영하는 기능은 구현하지 않았습니다.
- CLI 도움말·오류·XLSX 표시는 영어 기본입니다. 한국어 문서·설정 질문·연구 다음 작업 안내를 제공하며 전체 UI와 보고서의 한국어 전환은 아직 지원하지 않습니다.

## 실제 도입 전 필요한 자료

- 산업 상품의 SKU/모델 목록과 상품별 필수 식별자
- 실제 수집 대상 URL 3~5개와 각 URL의 허용된 수집 범위
- 사람이 확인한 정답 가격 표본과 통화·세금·배송·회원가 포함 여부
- 비식별 내부 CSV/PDF 샘플, 또는 승인된 내부 접근 경로와 계정 범위
- `price_basis`(개별/묶음), 포장 수량, 단가 계산, 가격 유형의 업무 정의
- 연결 대상이 Claude Desktop인지 ChatGPT인지와 해당 실행 환경

스캔 문서 OCR, 자율 브라우저 탐색, 규칙 복구, 예약 운영은 추가 설계·검증이 필요합니다. ERP 연동과 분석·모의계산 기능은 이번 범위에서 제외했습니다.

구현 범위와 검증 상태는 [IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)를 참고하세요.
