# 연구 워크스페이스 시작하기

[English](GETTING_STARTED.md) | [한국어](GETTING_STARTED.ko.md)

브라우저 UI는 Windows에서 `source-ledger.cmd`, macOS/Linux에서 `bash source-ledger.sh`로 실행하고 [브라우저 사용 안내](WEB_UI.ko.md)를 따라가세요. 아래 명령은 스크립트와 고급 설정에 계속 사용할 수 있습니다.

이 안내는 사용자가 주제·접근 가능한 출처·정확한 식별자·추출 규칙을 정하고 반복 수집하는 수동 설정 흐름입니다. 0.3의 [검색·규칙 제안·에이전트 실행](AUTOMATION.ko.md)을 이용하면 설정 작업을 줄일 수 있습니다.

아래 Windows 명령 프롬프트 블록은 `source-ledger.cmd`를, PowerShell 블록은 `.\source-ledger.cmd`를 사용합니다. macOS/Linux에서는 같은 CLI 인자와 함께 `bash source-ledger.sh`를 사용하세요.

## 1. 주제 입력

[README](../README.ko.md)의 설치를 마친 뒤 Windows에서 실행합니다.

```bat
source-ledger.cmd init --lang ko
```

macOS/Linux에서는 `bash source-ledger.sh init --lang ko`를 사용합니다. 산업군·상품 또는 상품군·대상 시장을 묻습니다. 기본 영어 안내는 `source-ledger.cmd init`입니다. 질문 없이 Windows에서 실행하려면 다음처럼 입력합니다.

```bat
source-ledger.cmd init --lang ko --industry "산업용 부품" --product "공정용 펌프" --market "한국"
```

기본 저장 위치는 Git에서 제외되는 `.sourceledger/research.json`입니다. 연구 명령의 `--workspace PATH`로 별도 주제를 만들 수 있으며 기존 워크스페이스는 덮어쓰지 않습니다. 자유롭게 입력한 상품명에서 SKU를 추정하지 않고 모르는 식별자는 빈 상태로 유지합니다. 분석 목적은 입력받지 않습니다.

## 2. 출처 등록과 후보 발견

예시 대신 실제 접근 권한이 있는 URL을 사용합니다.

```powershell
.\source-ledger.cmd source-add --url "https://example.com/catalog"
.\source-ledger.cmd research-status
.\source-ledger.cmd source-discover --source-id <출처_ID> --limit 25
```

`source-add`는 네트워크 호출 없이 등록만 합니다. 응답이나 `research-status`에서 출처 ID를 확인하세요. `source-discover`는 출처의 HTML 링크 또는 sitemap에서 같은 호스트의 후보를 찾아 발견 경로와 함께 저장합니다. HTTP 이후 필요하면 선택 설치된 브라우저도 시도합니다. Playwright 설치는 README를 참고하세요. 재귀적인 전체 사이트 수집은 아니며 한 번에 최대 1,000개, 워크스페이스 전체로 최대 1,000개 출처를 저장합니다.

후보에는 메뉴·다른 상품·중복 페이지가 섞일 수 있어 범위 확인과 추출 검증이 필요합니다. 승인된 내부 웹 출처는 `source-add --scope internal --url URL`로 명시합니다. 내부 파일은 이후 수집 초안에서 `kind: file`, `file_root`로 설정합니다.

기본 검색 공급자는 없습니다. 상태에 `search_provider_unconfigured`가 표시되며 주제를 입력했다고 유료 검색·모델 서비스를 자동 호출하지 않습니다.

## 3. 식별자와 수집 규칙 설정

합성 예시 대신 원문에서 확인한 식별자를 입력합니다.

```powershell
.\source-ledger.cmd product-set --identifier model=TEST-A
.\source-ledger.cmd draft --output .sourceledger/collection.draft.json
```

`--identifier KEY=VALUE`를 반복하면 식별자를 여러 개 지정합니다. 이 명령은 식별자 목록을 교체합니다. 선택적인 `--spec KEY=VALUE`는 필수 규격 목록을 교체하며 생략하면 이전 규격을 유지합니다.

초안에는 저장한 후보와 빈 추출 규칙이 포함됩니다. 다음을 확인하고 작성합니다.

- 관련 있고 승인된 출처만 남기고 지정 상품이 없는 카탈로그·메뉴 출처는 제거합니다.
- CSS 행·필드 선택자, 구조화된 데이터, 파일 열 매핑을 설정합니다. [verification.json](../examples/verification.json)은 합성 구조화 CSV를 사용하며, 보존된 HTML fixture는 테스트용이고 검증 데모 출처가 아닙니다.
- 상품 식별자·원문 가격·통화·거래 조건은 실제 원문에서 가져옵니다. 없는 사실을 채우지 않습니다.
- 비교 가능한 결과에는 단위·포장 수량·세금·가격 유형·`price_basis`(`pack` 또는 `each`)의 근거가 필요합니다. 조건이 없으면 비교에서 제외합니다.
- 브라우저 `recipe`는 명시적인 클릭·선택 동작으로 작성합니다. 이미 로그인된 전용 `profile_dir`을 지정할 수 있지만 로그인 갱신과 처음 보는 페이지는 개입이 필요합니다.

지원되는 JSON-LD는 기존 수집기가 자동 해석합니다. 후보 발견 기능이 새 선택자를 설계하거나 검증하는 것은 아닙니다. 연구 워크스페이스는 상품 주제 하나를 다루며 여러 정확한 상품은 수집 설정에서 구성합니다.

## 4. 검증 후 활성화

[verification.samples.json](../examples/verification.samples.json) 형식으로 정답 가격 표본을 준비할 수 있습니다. 값은 사람이 별도로 확인한 원문 기준이어야 하며 추정값이나 검색 요약을 사용하지 않습니다.

```powershell
.\source-ledger.cmd verify --config .sourceledger/collection.draft.json --samples .sourceledger/samples.json --receipt .sourceledger/receipt-v1.json
.\source-ledger.cmd activate --config .sourceledger/collection.draft.json --receipt .sourceledger/receipt-v1.json --output .sourceledger/active-v1.json
.\source-ledger.cmd run --config .sourceledger/active-v1.json
```

원문 일관성 검사만 필요하면 `--samples`를 생략할 수 있습니다. 이 경우 독립적인 정답 가격과의 일치를 확인한 것은 아닙니다. 표본은 출처·상품·정확한 소수 가격·통화를 대조합니다. 세금·배송·옵션 의미는 적절한 원문 매핑과 표본 검토가 필요합니다.

`verify`는 실제 수집 후 모든 상품에 출처가 연결되었는지, 모든 예정 작업이 확인 상태인지, 각 출처·상품 쌍에 최신·비교 가능 관측이 있는지 확인합니다. 보존한 근거를 다시 추출해 값도 재검증합니다. 실패해도 수집 데이터와 실패 사유를 남깁니다. 종료 코드는 통과 `0`, 불합격 `1`, 입력·실행 오류 `2`입니다.

`activate`는 설정·근거·관측·표본 파일을 다시 확인합니다. 검증 기록 유효시간은 24시간과 출처 신선도 설정 중 짧은 값이며 가격 자체의 유효기한도 확인합니다. 규칙 변경이나 실패 후에는 새 검증 기록 파일명을 사용하세요. 검증 기록과 활성 설정은 덮어쓰지 않습니다. 원자적 발행에는 하드 링크를 사용하므로 NTFS나 일반적인 Linux 파일시스템처럼 이를 지원하는 저장소가 필요합니다.

활성화는 절대 파일 경로가 포함된 독립 설정을 만듭니다. 예약 실행을 등록하거나 후보 워크스페이스를 직접 승격하지 않습니다. `research-status`는 후보 준비 상태, `status --config PATH --run-id ID`는 실제 수집 상태입니다. 수동 관리 설정은 활성화 없이도 기존 `run`으로 실행할 수 있습니다.

## 5. 데이터 누적

같은 연구의 `output_dir`을 유지하면 새 `run`마다 SQLite에 실행·관측이 누적됩니다. 미완료 실행은 `run --resume RUN_ID`로 이어갑니다. 근거는 SHA-256과 함께 보존되며 실행별 XLSX를 생성합니다. 없는 값은 빈칸입니다. CSV는 입력을 지원하며 출력은 SQLite와 XLSX입니다.

현재 확인 범위는 [구현 상태](IMPLEMENTATION_STATUS.md), 규칙 복구·예약 운영·클라이언트 연결 등 후속 작업은 [로드맵](ROADMAP.ko.md)을 참고하세요.
