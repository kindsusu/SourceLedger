# 제한된 자동화 (v0.3)

[English](AUTOMATION.md)

SourceLedger v0.3에는 범위가 제한된 `search`, `propose`, `agent` 명령이 있습니다. 공개 또는 명시적으로 승인한 내부 웹 출처의 규칙을 준비·검증하며 검색 결과는 공개 후보로만 저장합니다. 검색 요약이나 모델 출력은 가격으로 취급하지 않습니다. SourceLedger가 유료 API 키, 호스팅 서비스, 모델 다운로드, 스케줄러, 외부 서비스를 설치하지 않습니다.

실제 SearXNG 및 Ollama 서버 연동은 이 프로젝트에서 아직 검증하지 않았습니다. 현재는 모의 프로토콜 테스트만 통과했습니다. 운영하는 서버는 사용 전에 직접 확인해야 합니다.

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

## 3. 선택 사항: 로컬 Ollama 선택자 제안

`propose`는 저장된 출처 하나를 읽어 검토용 초안을 만듭니다. `--model-config`가 없거나 `--max-model-calls 0`이면 결정적 HTML/구조화 데이터 규칙만 사용합니다. 모델은 CSS 선택자만 제안합니다. 가격 값, JavaScript, 클릭, 브라우저 recipe, 명령, 활성화 결정을 낼 수 없습니다.

[model.ollama.json](../examples/model.ollama.json)을 복사해 `YOUR_INSTALLED_LOCAL_MODEL`을 이미 본인의 Ollama 서버에 설치한 모델명으로 바꿉니다. 모델 설정은 명시적이며 로컬 전용입니다.

```json
{
  "provider": "ollama",
  "endpoint": "http://127.0.0.1:11434/api/chat",
  "model": "YOUR_INSTALLED_LOCAL_MODEL",
  "local_only": true,
  "timeout_seconds": 30,
  "max_input_chars": 30000,
  "num_predict": 1200
}
```

Ollama 서버가 실행되는 장비에서 `OLLAMA_NO_CLOUD=1`을 설정하고 서버를 수동으로 다시 시작하세요. 루프백 URL만으로는 공급자가 로컬임을 증명할 수 없습니다. SourceLedger는 임의의 모델 프록시를 허용하지 않습니다.

```powershell
source-ledger propose --source-id SOURCE_ID --output-dir .sourceledger/proposals/SOURCE_ID --max-model-calls 0
source-ledger propose --source-id SOURCE_ID --output-dir .sourceledger/proposals/SOURCE_ID-model --model-config .sourceledger/model.ollama.json --max-model-calls 1
```

매번 새 출력 디렉터리를 사용합니다. 결과는 검토용 미리보기/초안이며 검증되거나 활성화된 것이 아닙니다. 요청 형식은 [Ollama chat API](https://docs.ollama.com/api/chat), 서버 운영은 [Ollama FAQ](https://docs.ollama.com/faq/)를 참고하세요.

## 4. 제한된 배치 실행

에이전트는 실행 상태를 체크포인트로 저장합니다. 기본 모델 호출 횟수는 0회입니다. `--max-steps`는 제한된 출처 단계 뒤에 멈추며 `--resume`은 변경된 인자를 받지 않고 저장된 입력과 예산을 다시 사용합니다. `--max-seconds`는 협조적으로 적용하는 총 작업 시간 한도이며 프로세스를 강제 종료하지는 않습니다.

```powershell
source-ledger agent --run-dir .sourceledger/runs/run-001 --max-sources 3 --max-seconds 120
source-ledger agent --run-dir .sourceledger/runs/run-001 --resume
```

같은 배치에서 먼저 검색하려면 `--search-config .sourceledger/search.searxng.json`을 추가합니다. 로컬 모델 호출을 하나 허용하려면 `--model-config .sourceledger/model.ollama.json --max-model-calls 1`을 함께 추가합니다. 검색 대신 저장된 출처 ID를 하나 이상 지정할 수 있습니다.

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
