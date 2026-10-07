# 로컬 AI 도구 연결

[English](ASSISTANT_CONNECTIONS.md) | [한국어](ASSISTANT_CONNECTIONS.ko.md)

0.5의 [브라우저 UI](WEB_UI.ko.md)에서는 **Connections**에서 설정을 만들고 worker를 시작할 수 있습니다. UI와 AI 도구는 같은 워크스페이스를 공유하며 stdio MCP 자체는 웹 서버와 독립적입니다. 생성 설정을 직접 설치하려면 아래 CLI 흐름을 사용하세요.

SourceLedger는 하나의 워크스페이스를 Codex, Claude Code, Claude Desktop에 로컬 stdio MCP 서버로 연결할 수 있습니다. MCP 연결 자체는 로컬에서 동작하며 웹 서버를 열거나 워크스페이스를 업로드하지 않고 유료 검색·모델 공급자를 켜지 않습니다. 연결된 AI는 자체 검색·브라우저 도구와 해당 요금제·도구 제한을 따릅니다. 별도로 브라우저 UI에서 설치된 Codex CLI 또는 Claude Code CLI로 출처 추천을 명시적으로 실행할 수 있습니다. 수집 worker는 MCP와 별도 프로세스이므로 이미 worker가 맡은 작업은 MCP 클라이언트를 닫아도 종료되지 않습니다.

이 문서는 생성되는 설정과 CLI 계약을 설명합니다. 모든 Codex·Claude GUI, 클라우드 세션, 웹·모바일 화면, 호스트 브라우저 인계를 이 프로젝트에서 실제로 검증했다는 뜻은 아닙니다.

## 설치와 연결

저장소 폴더에서 다음을 실행합니다. macOS/Linux에서는 두 `.cmd` 명령을 각각 `bash setup.sh`, `bash source-ledger.sh`로 바꿉니다.

```bat
setup.cmd --mcp --browser
source-ledger.cmd connect --client codex --install
source-ledger.cmd assistant start --workspace-root .sourceledger
```

다른 클라이언트는 `--client claude-code` 또는 `--client claude-desktop`을 사용합니다. 설정 설치 뒤 클라이언트를 다시 시작하거나 연결을 새로고침하세요. 그 다음 SourceLedger 연결에 산업군·상품·시장을 설정하고, 허가된 출처만 등록하도록 요청합니다.

`connect`의 기본 워크스페이스는 `.sourceledger`, 서버 이름은 `sourceledger`입니다. 저장소 가상환경의 절대 Python 경로를 사용하므로 클라이언트가 현재 셸이나 `PATH`에 의존하지 않습니다.

## 생성 설정과 안전한 설치

`--install` 없이 실행하면 검토할 수 있는 설정 조각만 만듭니다.

```bat
source-ledger.cmd connect --client claude-desktop
```

기본 출력과 설치 대상은 다음과 같습니다.

| 클라이언트 | 생성 파일 | 기본 설치 대상 |
| --- | --- | --- |
| Codex | `connections/codex.sourceledger.toml` | `CODEX_HOME/config.toml`, 없으면 `~/.codex/config.toml` |
| Claude Code | `connections/claude-code.sourceledger.json` | `--project-dir` 또는 현재 폴더의 `.mcp.json` |
| Claude Desktop | `connections/claude-desktop.sourceledger.json` | Windows: `%APPDATA%/Claude/claude_desktop_config.json`; macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` |

`--output DIR`, `--name NAME`으로 여러 연결을 분리할 수 있습니다. 이 저장소가 아닌 Claude Code 프로젝트에 설치할 때는 대상 프로젝트를 명시하세요.

```bat
source-ledger.cmd connect --client claude-code --project-dir "C:\work\my-project" --install
```

`--config-file PATH`는 클라이언트 설정 파일을 직접 지정하며 `--install`과 함께만 쓸 수 있습니다. Windows·macOS 이외 운영체제에서 Claude Desktop을 연결할 때 이 옵션이 필요합니다.

설치는 없는 `sourceledger` 서버 항목만 병합합니다. 다른 설정을 유지하고, 대상 파일 옆에 바이트 단위 백업(`.sourceledger-<id>.bak`)을 남기며, 같은 이름에 다른 설정이 있으면 덮어쓰지 않고 거절합니다. 그 경우 다른 이름을 쓰거나 생성된 설정을 직접 검토해 병합하세요. 프로젝트 `.mcp.json`과 백업 파일은 Git이 추적하지 않습니다.

## worker와 작업 수명

AI 클라이언트 밖의 일반 터미널에서 worker를 시작합니다.

```bat
source-ledger.cmd assistant start --workspace-root .sourceledger
source-ledger.cmd assistant status --workspace-root .sourceledger
source-ledger.cmd assistant jobs --workspace-root .sourceledger
```

기존 MCP `queue_*` 작업은 범위가 정해진 작업만 대기열에 넣고 worker를 자동으로 시작하지 않습니다. 새 `start_research_plan`은 승인한 계획의 worker를 시작합니다. 그 밖의 작업은 worker가 멈춘 상태에서도 받을 수 있으며, worker를 시작할 때까지 `queued` 상태로 남습니다. 작업은 워크스페이스에 저장되어 한 번에 하나씩 실행됩니다. 반환된 작업 ID로 상태를 보거나, 중단된 작업만 명시적으로 재개할 수 있습니다.

```bat
source-ledger.cmd assistant job --workspace-root .sourceledger --job-id <작업_ID>
source-ledger.cmd assistant resume --workspace-root .sourceledger --job-id <작업_ID>
source-ledger.cmd assistant stop --workspace-root .sourceledger
```

`stop`은 중지 요청을 기록하고 즉시 반환합니다. worker는 현재 작업이 끝난 뒤 종료되므로 `assistant status`로 중지 상태를 확인하세요. worker가 비정상 종료하면 실행 중이던 작업은 `interrupted`가 되며 자동 재실행하지 않습니다.

## 로컬 MCP 서버의 범위

새 연구 계획 경로에는 `list_research_plans`, `get_research_plan`, `create_research_plan`, `submit_research_preview`, `update_research_plan`, `confirm_research_plan`, `start_research_plan`, `generate_research_preview`의 도구 8개가 추가되어 assistant MCP 도구는 총 27개입니다. 짧거나 여러 줄의 상세 요청을 그대로 저장합니다. 연결된 AI 앱은 자체 검색·브라우저 도구로 실제 근거를 살펴본 뒤 `submit_research_preview`로 URL과 이유를 제출할 수 있으며, 모델은 호스트 앱이 관리합니다. 또는 Codex CLI·Claude Code CLI 공급자와 모델을 명시적으로 선택해 `generate_research_preview`를 실행할 수 있습니다. 두 경로의 결과는 제한된 미검증 출처 근거이며 가격 관측이 아닙니다. 사용자는 후보를 추가·제거·선택하고 산업군·상품·시장과 표시된 수정본을 승인한 뒤 `start_research_plan`으로 수집을 시작합니다. 정확한 상품 식별자는 이 흐름에서 고급 선택 사항입니다.

승인한 계획은 페이지·시간 한도 안에서 선택 URL을 수집하고 방문·미처리 범위를 기록하며 SQLite 원문 근거와 XLSX 보고서를 만듭니다. 선택 호스트의 관련 없는 탐색 링크도 한도 안에서 방문할 수 있습니다. 일반 JSON-LD Product/Offer는 여러 상품 종류의 원문 가격·속성을 보존할 수 있지만 화면 표시 근거가 없는 값은 검토 대상으로 남습니다. 알려진 어댑터의 가격도 자연어 연구 범위와 상업 조건을 독립적으로 확인하지 않았으므로 검토 대상입니다. 임의 페이지 레이아웃과 조건의 의미 검증은 지원하지 않으며 없는 값은 추정하지 않습니다. 미리보기 내용은 범위를 넓히거나 가격 수집을 지시하는 명령이 아닙니다.

UI에서 회사·사이트 URL을 입력하면 바로 출처로 등록합니다. 회사명만 입력하거나 출처 키워드를 입력하면 추천 요청을 저장합니다. **Sources**의 요청 문구를 연결된 AI에 복사해 전달해야 하며, 저장만으로 AI 클라이언트가 시작되지는 않습니다. AI는 `list_source_recommendation_requests`를 호출하고, 필요하면 `request_source_recommendations(query, kind)`로 요청을 만들 수 있습니다. 자체 검색 도구로 실제 사이트를 찾은 뒤 `submit_source_recommendations(request_id, candidates, note)`를 호출합니다. 각 후보에는 `name`, `url`, `reason`, `evidence_url`이 필요합니다. 검색이 불가능하거나 결과가 없으면 빈 후보 목록과 설명을 제출할 수 있습니다. 사용자는 UI에서 이유·근거를 검토하고 체크박스로 등록할 후보를 고릅니다. MCP에는 사용자를 대신해 추천을 선택하는 도구가 없습니다.

요청 생성과 추천 제출에는 수집 worker가 필요하지 않습니다. 제출한 후보는 등록된 출처·확인된 관측과 별도이며 AI가 제공한 이유와 근거 URL은 미검증입니다. 후보 선택만으로 수집하지 않습니다. 기존 안내형 연구는 UI에서 등록된 출처를 선택하고 worker를 시작해 대기열에 넣습니다(명시적 출처 최대 50개, 120초). 기존 워크스페이스는 하나의 상품을 출발점으로 삼습니다. 새 연구 계획은 상품 범위를 기술할 수 있으나 수집은 페이지 한도로 제한되며 상품 목록 전체를 자동 수집하지 않습니다.

기존 도구는 워크스페이스 범위의 첫 설정·출처 관리를 제공하고, `discover`, `propose`, `agent`, `collect_sites`, `verify`, `run`, `export` 작업을 대기열에 넣습니다. 작업 상태·관측·XLSX 보고서 메타데이터와 완료된 보고서의 등록된 로컬 XLSX resource도 반환합니다. 입력은 정해진 한도 안에서 워크스페이스 내부에만 남습니다. 임의 명령과 워크스페이스 밖 경로는 거절합니다. 기존 `queue_*` 도구는 공급자를 설정하거나 모델을 호출하지 않습니다. 새 `generate_research_preview`는 명시적으로 선택한 로컬 Codex CLI·Claude Code CLI 모델 미리보기를 위한 별도 MCP 경로이며, 브라우저 UI에도 선택형 CLI 추천 실행이 있습니다. 출처 후보나 규칙 제안은 확인된 가격 관측이 아닙니다.

생성되는 서버 명령은 다음과 같습니다.

```text
<저장소-가상환경-Python> -m su_crawler serve-assistant --workspace-root <절대-워크스페이스-경로>
```

stdio만 사용합니다. MCP 클라이언트가 이 명령을 시작하므로 같은 터미널에서 `serve-assistant`를 직접 실행하지 마세요. 기존 `serve-mcp --config ...`는 이미 고정된 수집 설정용으로 계속 제공하며 README에 별도로 설명합니다.

## 검증 범위와 한계

오프라인 테스트는 JSON/TOML 생성, 안전한 병합과 백업, worker 수명, 대기 작업, 워크스페이스 경로 경계, MCP 서비스 계약을 다룹니다. 유료 서비스나 외부 전송은 필요하지 않습니다. 추가로 설치된 Codex CLI가 한글·공백 워크스페이스 경로를 포함한 격리 생성 설정을 해석하는 것과, 설치된 Claude Code CLI가 프로젝트 설정을 해석하는 것(클라이언트의 일반 승인 단계는 미완료)을 확인했습니다. Claude Desktop은 JSON 왕복 검사만 했습니다. 이는 Codex·Claude Code·Claude Desktop GUI 실제 연결을 증명하지 않습니다. 클라이언트 설정 UI가 다르면 공식 로컬 MCP 안내를 확인하세요: [Codex](https://developers.openai.com/codex/mcp), [Claude Code](https://code.claude.com/docs/en/mcp), [MCP 로컬 서버 안내](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## 웹 UI CLI 추천

브라우저 UI에는 저장된 출처 추천 요청을 명시적으로 실행하는 **Run in web UI** 기능도 있습니다. **Connections**에서 설치된 Codex CLI 또는 Claude Code CLI를 고르고, 공급자 기본 모델 또는 모델 ID를 지정한 뒤 타임아웃(30~600초, 기본 180초)을 저장합니다. SourceLedger가 실행되는 컴퓨터에 CLI 설치와 로그인이 필요합니다. 자세한 내용은 공급자의 [Codex CLI 안내](https://developers.openai.com/codex/cli/)와 [Claude Code 설치 안내](https://code.claude.com/docs/en/setup)를 참고하세요. 가능 상태에는 설치 여부만 나타나며 인증은 작업 실행 시 확인합니다. CLI 기본 인증과 계정 사용 조건이 적용됩니다. SourceLedger는 API 키를 저장하거나 CLI 설치·로그인을 하지 않고, 다른 공급자로 자동 전환하지 않습니다. 이 설정은 열려 있는 데스크톱 대화 모델을 바꾸지 않습니다.

요청 실행은 로컬 worker 작업을 대기열에 넣습니다. 결과는 Sources에서 사람이 검토하고 선택해야 하는 미검증 추천 후보이며, 가격 수집 작업을 시작하거나 가격 관측을 만들지 않습니다. 검색 중 공급자 CLI가 웹 페이지에 접근할 수 있습니다. 기존 수집 모델 호출은 계속 선택 사항이며 기본값은 0회입니다.

이 연결은 클라우드 배포가 아닙니다. Codex/Claude 웹·모바일 접근, 원격 산출물 다운로드, 호스트 브라우저 세션 직접 재사용, 예약 실행, 무인 복구는 이번 구현 범위 밖입니다.
