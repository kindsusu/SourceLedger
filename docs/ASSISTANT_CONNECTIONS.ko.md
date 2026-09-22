# 로컬 AI 도구 연결

[English](ASSISTANT_CONNECTIONS.md) | [한국어](ASSISTANT_CONNECTIONS.ko.md)

SourceLedger 0.4는 하나의 워크스페이스를 Codex, Claude Code, Claude Desktop에 로컬 stdio MCP 서버로 연결할 수 있습니다. 이 연결은 로컬에서만 동작합니다. 웹 서버를 열거나 워크스페이스를 업로드하지 않으며, 유료 검색·모델 공급자를 켜지 않습니다. 수집 worker는 MCP와 별도 프로세스이므로 AI 클라이언트를 닫아도 진행 중인 작업이 바로 종료되지 않습니다.

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

MCP 서버는 범위가 정해진 작업만 대기열에 넣고 worker를 자동으로 시작하지 않습니다. worker가 멈춘 상태에서도 작업을 받을 수 있으며, worker를 시작할 때까지 작업은 `queued` 상태로 남습니다. 작업은 워크스페이스에 저장되어 한 번에 하나씩 실행됩니다. 반환된 작업 ID로 상태를 보거나, 중단된 작업만 명시적으로 재개할 수 있습니다.

```bat
source-ledger.cmd assistant job --workspace-root .sourceledger --job-id <작업_ID>
source-ledger.cmd assistant resume --workspace-root .sourceledger --job-id <작업_ID>
source-ledger.cmd assistant stop --workspace-root .sourceledger
```

`stop`은 중지 요청을 기록하고 즉시 반환합니다. worker는 현재 작업이 끝난 뒤 종료되므로 `assistant status`로 중지 상태를 확인하세요. worker가 비정상 종료하면 실행 중이던 작업은 `interrupted`가 되며 자동 재실행하지 않습니다.

## 로컬 MCP 서버의 범위

서버는 워크스페이스 범위의 첫 설정·출처 관리를 제공하고, `discover`, `propose`, `agent`, `collect_sites`, `verify`, `run`, `export` 작업을 대기열에 넣습니다. 작업 상태·관측·XLSX 보고서 메타데이터와 완료된 보고서의 등록된 로컬 XLSX resource도 반환합니다. 입력은 정해진 한도 안에서 워크스페이스 내부에만 남습니다. 이 연결은 임의 명령, 워크스페이스 밖 경로, 공급자 설정, 모델 호출을 거절합니다. 출처 후보나 규칙 제안은 확인된 가격 관측이 아닙니다.

생성되는 서버 명령은 다음과 같습니다.

```text
<저장소-가상환경-Python> -m su_crawler serve-assistant --workspace-root <절대-워크스페이스-경로>
```

stdio만 사용합니다. MCP 클라이언트가 이 명령을 시작하므로 같은 터미널에서 `serve-assistant`를 직접 실행하지 마세요. 기존 `serve-mcp --config ...`는 이미 고정된 수집 설정용으로 계속 제공하며 README에 별도로 설명합니다.

## 검증 범위와 한계

오프라인 테스트는 JSON/TOML 생성, 안전한 병합과 백업, worker 수명, 대기 작업, 워크스페이스 경로 경계, MCP 서비스 계약을 다룹니다. 유료 서비스나 외부 전송은 필요하지 않습니다. 추가로 설치된 Codex CLI가 한글·공백 워크스페이스 경로를 포함한 격리 생성 설정을 해석하는 것과, 설치된 Claude Code CLI가 프로젝트 설정을 해석하는 것(클라이언트의 일반 승인 단계는 미완료)을 확인했습니다. Claude Desktop은 JSON 왕복 검사만 했습니다. 이는 Codex·Claude Code·Claude Desktop GUI 실제 연결을 증명하지 않습니다. 클라이언트 설정 UI가 다르면 공식 로컬 MCP 안내를 확인하세요: [Codex](https://developers.openai.com/codex/mcp), [Claude Code](https://code.claude.com/docs/en/mcp), [MCP 로컬 서버 안내](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

이 연결은 클라우드 배포가 아닙니다. Codex/Claude 웹·모바일 접근, 원격 산출물 다운로드, 호스트 브라우저 세션 직접 재사용, 예약 실행, 무인 복구는 이번 구현 범위 밖입니다.
