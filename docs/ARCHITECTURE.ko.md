# SourceLedger 런타임 아키텍처

[English](ARCHITECTURE.md) · [다이어그램 소스](architecture/sourceledger.architecture.json) · [대화형 다이어그램](architecture/sourceledger.html) · [PNG 미리보기](architecture/sourceledger.png) · [출처 기록](architecture/PROVENANCE.md) · [검증](architecture/verification.json)

SourceLedger는 근거와 수집 결과를 탐색·규칙 제안과 분리합니다. SQLite가 시스템 기록이며 XLSX 워크북은 한 실행의 결과에서 만드는 사람이 읽는 보고서입니다. 이 문서는 구현된 로컬 경로를 설명하며, 외부 공급자의 실운영 검증을 주장하지 않습니다.

## 다이어그램 보기

대화형 다이어그램은 최신 브라우저에서 `docs/architecture/sourceledger.html`을 로컬로 열어 확인합니다. GitHub는 JSON 소스는 표시하지만 독립 HTML을 실행하지 않습니다. 저장소에서는 PNG 미리보기를 보고, 테마 전환·이동/확대·검색·포커스·관계 추적·내보내기 기능은 HTML을 내려받아 로컬에서 여세요.

작성된 다이어그램은 코드와 명령 표면의 이름을 맞추기 위해 영어 레이블을 사용합니다. 이 안내는 영문 문서와 같은 내용을 한국어로 제공합니다.

## 런타임 경로

주요 경로는 수동 CLI를 통한 제한된 조사입니다. 별도의 MCP/worker 카드는 이미 설정된 수집을 위한 대체 경로를 보여 줍니다.

1. **수동 CLI.** 운영자는 조사 워크스페이스를 초기화하고 정확한 식별자와 출처 후보를 입력한 뒤 `search`, `propose`, `agent`, `verify`, `activate` 또는 설정된 수집을 실행할 수 있습니다. 명령 분기는 [`su_crawler/cli.py`](../su_crawler/cli.py)에 있습니다.
2. **로컬 MCP.** 로컬 MCP 서버는 고정된 수집 설정만 받습니다. `start_collection`은 로컬 dispatch receipt를 쓰고, 별도로 시작한 worker가 이를 소비해 같은 수집 파이프라인을 호출합니다. [`su_crawler/mcp_server.py`](../su_crawler/mcp_server.py), [`su_crawler/worker.py`](../su_crawler/worker.py)를 보세요.

제한된 agent는 입력·한도·출처 작업 체크포인트를 보존합니다. agent가 고르는 대상은 웹 출처뿐입니다. 선택 전에 설정된 SearXNG 요청을 한 번 수행할 수 있으며, 그 뒤 검토 전용 출처 규칙 제안을 만듭니다. 제안 단계 후에는 하나의 새 통합 검증 수집을 수행합니다. 제어기는 [`su_crawler/agent.py`](../su_crawler/agent.py), 선택형 검색은 [`su_crawler/search.py`](../su_crawler/search.py), 제안은 [`su_crawler/proposals.py`](../su_crawler/proposals.py)에 구현되어 있습니다.

`verify`는 정확한 설정을 실행하고 SQLite의 완료 실행을 읽어 보존 근거와 현재 비교 가능 관측을 검사한 뒤 로컬 receipt를 만듭니다. 선택적인 known sample은 상품·가격을 독립적으로 대조합니다. 수동으로 설정한 `run`에는 activation이 필요하지 않습니다. `activate`는 독립 active config를 쓰기 전에 receipt·설정·sample·근거를 다시 검사하며, 자동 agent activation은 선택한 모든 출처/상품의 known sample을 요구합니다. receipt는 로컬 감사 기록이지 서명된 증명서가 아닙니다. [`su_crawler/activation.py`](../su_crawler/activation.py), [`su_crawler/agent.py`](../su_crawler/agent.py)를 보세요.

수집 파이프라인은 제한되고 재개 가능한 출처/상품 작업을 실행하고, 근거를 저장하며, 관측을 추출·검증해 SQLite에 시도와 관측을 기록하고 XLSX 보고서를 작성합니다. 공통 설정 경로는 승인된 웹 페이지 또는 로컬 파일을 받을 수 있지만 agent가 내부 파일을 자동 탐색하지는 않습니다. 구현은 [`su_crawler/pipeline.py`](../su_crawler/pipeline.py), [`su_crawler/storage.py`](../su_crawler/storage.py), [`su_crawler/report.py`](../su_crawler/report.py)에 있습니다.

## 범위와 공급자 상태

- SearXNG 검색은 선택 사항입니다. 공급자 설정이 없으면 검색 요청은 0회이며, 반환 snippet은 후보의 발견 근거일 뿐 가격이 아닙니다.
- 규칙 제안은 기본적으로 결정적 규칙을 사용합니다. 설정된 로컬 Ollama endpoint는 selector만 제안할 수 있고, 출력은 새 출처 검증을 다시 통과해야 합니다.
- HTTP 수집과 설정된 Playwright fallback은 구현된 로컬 수집 경로입니다.
- 실제 SearXNG 서버와 Ollama 모델은 이 프로젝트에서 검증하지 않았습니다. Crawl4AI의 실사이트 사용은 선택 기능이며 미검증입니다. 유료 Ultimate Web Scraper MCP와 다른 참고 도구는 연결된 런타임 서비스가 아닙니다.

검증 범위와 공급자 준비 조건은 [상태](IMPLEMENTATION_STATUS.md), [자동화](AUTOMATION.ko.md)를 보세요.

## 다이어그램 소스와 재생성

다이어그램 자산은 함께 둡니다.

| 자산 | 용도 |
| --- | --- |
| [`sourceledger.architecture.json`](architecture/sourceledger.architecture.json) | 검토 가능한 형식화 Archify 소스 기준 |
| [`sourceledger.html`](architecture/sourceledger.html) | 독립 실행 대화형 HTML 결과물 |
| [`sourceledger.png`](architecture/sourceledger.png) | 저장소 브라우저용 정적 미리보기 |
| [`PROVENANCE.md`](architecture/PROVENANCE.md), [`verification.json`](architecture/verification.json) | 출처·버전 기록과 생성된 검증 근거 |

Archify는 SourceLedger 앱 의존성이 아니라 로컬 Codex 스킬입니다. 이 워크스테이션에는 `tt-a1i/archify`의 commit `72c750bb070d95171dbb2244e5b62b1b7da69c12`에서 온 Archify `v2.17.0-dev.1`이 설치되어 있습니다. 저장소를 clone해도 기여자에게 스킬이 자동 설치되지 않습니다. 각 기여자는 Codex 스킬 설정으로 별도로 설치하거나 이미 설치된 로컬 사본을 사용해야 합니다. Codex 환경에서는 내장 installer로 이 고정된 소스를 재현할 수 있습니다.

```powershell
$archifyHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$installer = Join-Path $archifyHome "skills\.system\skill-installer\scripts\install-skill-from-github.py"
python $installer --repo tt-a1i/archify --path archify --ref 72c750bb070d95171dbb2244e5b62b1b7da69c12
```

Archify는 문서 빌드 경로로 동작합니다. agent가 코드 근거를 확인하고 형식화 JSON 다이어그램을 작성하면 validator가 검사하고, self-contained HTML/SVG를 결정적으로 배포합니다. 그 뒤 reviewer 또는 browser check가 배포된 결과물을 확인합니다. 이 과정은 SourceLedger를 런타임에 자동 감시하지 않으며, 레이아웃 검증이 작성한 토폴로지가 사실임을 증명하지도 않습니다. 유지보수자가 JSON을 코드와 맞춰야 합니다.

기본 결정적 렌더링에는 Node.js 18 이상과 설치된 스킬 패키지가 필요하며, npm 설치나 MCP/유료 서비스는 필요하지 않습니다. 이 워크스테이션에서는 Node.js `v24.18.0`으로 `doctor`가 성공했습니다. 이는 로컬 스킬 환경을 진단한 결과이며 다이어그램 내용의 검증은 아닙니다. LLM agent에게 다이어그램 분석·수정을 요청하는 일은 별도 작업이므로 해당 agent의 모델 토큰을 사용할 수 있습니다. Archify의 선택적 browser check 역시 결정적 검증과 별개이며 브라우저 실행 환경이 필요합니다.

PowerShell에서는 설치된 CLI 경로를 지정한 뒤 고정된 JSON 소스를 진단·검증·배포합니다.

```powershell
$archifyHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$archifyCli = Join-Path $archifyHome "skills\archify\bin\archify.mjs"
node $archifyCli doctor
node $archifyCli validate architecture docs/architecture/sourceledger.architecture.json --quality showcase --json
node $archifyCli deliver architecture docs/architecture/sourceledger.architecture.json docs/architecture/sourceledger.html --quality showcase --json
node $archifyCli visual-check docs/architecture/sourceledger.html --json
```

`visual-check`은 같은 JSON bytes에 대해 `deliver`가 성공한 뒤에만 실행합니다. `deliver`는 결정적 산출물 검사를, `visual-check`은 자동 browser evidence를 기록합니다. 시각적 완성도는 사람 또는 이미지를 읽을 수 있는 검토가 별도로 판단합니다. 이 세 주장은 서로 다릅니다. 다이어그램이 바뀌면 배포된 로컬 HTML에서 PNG 미리보기를 다시 만들고, commit 전 결과 파일을 검토하세요. 현재 검증·browser evidence 상태는 생성된 receipt로 확인하며, 이 안내 자체가 그 결과를 대체하지는 않습니다.

## 유지보수 규칙

런타임 컴포넌트나 관계가 바뀌면 JSON을 수정하고 HTML/미리보기를 다시 생성합니다. 이 저장소가 외부 서비스를 검증하고 연결하기 전까지는 구현 경계 밖에 둡니다. 다이어그램은 자격 증명, 브라우저 프로필, 조사 워크스페이스, 데이터베이스, 수집 문서, 생성 보고서를 저장소 산출물로 설명하지 않습니다.
