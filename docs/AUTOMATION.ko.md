# 제한된 자동화 (v0.3)

[English](AUTOMATION.md)

SourceLedger v0.3에는 범위가 제한된 `search`, `propose`, `agent` 명령이 있습니다. 공개 또는 명시적으로 승인한 내부 웹 출처의 규칙을 준비·검증하며 검색 결과는 공개 후보로만 저장합니다. 검색 요약이나 도우미 출력은 가격으로 취급하지 않습니다. SourceLedger가 유료 API 키, 호스팅 서비스, 모델 다운로드, 스케줄러, 외부 서비스를 설치하지 않습니다.

실제 SearXNG 서버 연동은 이 프로젝트에서 아직 검증하지 않았습니다. 현재는 모의 프로토콜 테스트만 통과했습니다. 운영하는 서버는 사용 전에 직접 확인해야 합니다.

## 1. 작업공간 준비

작업공간을 만들고 정확한 식별자를 설정합니다. `research-status` 또는 `source-add`의 JSON 결과에서 출처 ID를 확인합니다.

```powershell
source-ledger init --industry "Industrial components" --product "Process pump" --market "South Korea"
source-ledger product-set --identifier model=PX-100
source-ledger source-add --url "https://vendor.example/catalog/pump"
source-ledger research-status
```

`source-add`는 원문을 가져오지 않고 승인된 명시 URL만 저장합니다. 반환된 `id`가 `source-discover`, `propose`에 쓰는 `SOURCE_ID`입니다.

## 2. 선택 사항: SearXNG 후보 검색

기본적으로 `search`에는 공급자 설정이 없으며 `--provider-config` 없이는 네트워크 호출이 0회입니다. 설정한 SearXNG 한 곳에만 한 번 요청하고 조건을 만족한 HTTP(S) 결과를 공개 출처 후보로 저장합니다. 결과 페이지를 가져오지 않으며 검색 요약을 가격으로 기록하지 않습니다.

[search.searxng.json](../examples/search.searxng.json)을 복사해 수정합니다. 예시는 8080 포트의 루프백 서버이며 SearXNG를 설치하거나 시작하지 않습니다. 그 루프백 엔드포인트에는 `allow_private_endpoint: true`가 필요합니다. 사설이 아닌 엔드포인트는 HTTPS를 쓰고, 필요하면 `allowed_result_domains`으로 결과 호스트를 제한합니다.

```powershell
source-ledger search --provider-config .sourceledger/search.searxng.json --limit 10
source-ledger research-status
```

SearXNG 서버에서 JSON 검색 API를 켜야 합니다. API는 `q`, `format=json`을 받고, `403`은 다른 서비스로 재시도하지 않고 사용 불가로 보고됩니다. [SearXNG Search API 문서](https://docs.searxng.org/dev/search_api.html)를 참고하세요.

## 3. 출처 기반 규칙 제안

`propose`는 저장된 출처 하나를 읽어 검토용 초안을 만듭니다. 제안은 결정적입니다. 구조화된 데이터(JSON-LD)와 의미가 명시된 HTML 규칙을 가져온 페이지에서 검증하며, 관측하지 못한 조건은 비워 둡니다. 선택적으로 연결된 호스트 도우미(MCP를 통한 Claude)가 규칙을 제안할 수 있으며, 어떤 제안이든 활성화 전에 새 출처·정답 표본 검증을 거쳐야 합니다.

```powershell
source-ledger propose --source-id SOURCE_ID --output-dir .sourceledger/proposals/SOURCE_ID
```

매번 새 출력 디렉터리를 사용합니다. 결과는 검토용 미리보기/초안이며 검증되거나 활성화된 것이 아닙니다.

## 4. 제한된 배치 실행

에이전트는 실행 상태를 체크포인트로 저장합니다. `--max-steps`는 제한된 출처 단계 뒤에 멈추며 `--resume`은 변경된 인자를 받지 않고 저장된 입력과 예산을 다시 사용합니다. `--max-seconds`는 협조적으로 적용하는 총 작업 시간 한도이며 프로세스를 강제 종료하지는 않습니다.

```powershell
source-ledger agent --run-dir .sourceledger/runs/run-001 --max-sources 3 --max-seconds 120
source-ledger agent --run-dir .sourceledger/runs/run-001 --resume
```

같은 배치에서 먼저 검색하려면 `--search-config .sourceledger/search.searxng.json`을 추가합니다. 검색 대신 저장된 출처 ID를 하나 이상 지정할 수 있습니다.

```powershell
source-ledger agent --run-dir .sourceledger/runs/run-002 --source-id SOURCE_ID --max-sources 1 --max-seconds 120
```

출처별 미리보기·초안을 만든 뒤 전체 출처를 한 번에 새로 수집·검증합니다. 작업공간 상위의 `outputs`에 공통 SQLite와 실행별 XLSX를 저장하며 가격을 얻지 못한 출처도 포함합니다. 기본 위치는 `.sourceledger/outputs`입니다. 결과 JSON의 `collection.report_path`, `collection.config_path`, `collection.receipt_path`에서 경로를 확인하세요. 이후 `run --config PATH`로 검증한 설정을 재사용하면 출처 탐색과 모델 작업을 반복하지 않아도 됩니다.

새 명령의 종료 코드는 성공한 검색·제안·완료 또는 의도적으로 일시 정지한 실행 `0`, 검토·미설정·차단·한도 소진 `1`, 입력·실행 오류 `2`입니다. 한도를 소진한 실행은 새 실행 폴더와 명시적인 새 한도가 필요합니다. 일시 정지 후 재개는 기존 한도를 유지합니다.

## 5. 활성화에는 정답 표본 필요

`--activate`는 더 엄격합니다. 정답 가격 표본 파일을 제공하고 선택한 모든 출처를 포함해야 합니다. 현재 형식은 [verification.samples.json](../examples/verification.samples.json)입니다. 각 표본은 `source_id`, `product_id`, 소수 문자열 `price`, `currency`를 갖고, 파일 래퍼는 `source-ledger/known-samples/v1`입니다.

```powershell
source-ledger agent --run-dir .sourceledger/runs/run-003 --source-id SOURCE_ID --samples .sourceledger/samples.json --activate --max-sources 1 --max-seconds 120
```

정답 표본은 독립적이고 현재의 사람 확인 원문에서 가져와야 합니다. 실패·불완전·만료·불일치 근거는 검토 자료로 남고, SourceLedger는 빠진 값을 만들어 내거나 출처를 활성화하지 않습니다.
