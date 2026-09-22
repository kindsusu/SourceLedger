# 브라우저 UI

[English](WEB_UI.md) | [한국어](WEB_UI.ko.md)

SourceLedger 0.5는 기존 연구 워크스페이스, 지속 작업, SQLite 원장, XLSX 보고서를 사용하는 로컬 브라우저 인터페이스를 추가합니다. 한 대의 PC에서 SourceLedger를 시작하는 가장 쉬운 방법입니다. 자동화와 고급 작업에는 기존 CLI와 Codex·Claude 로컬 MCP 연결을 계속 사용할 수 있습니다.

## 시작

저장소 core를 설치한 뒤 인수 없이 실행합니다.

```bat
setup.cmd
source-ledger.cmd
```

PowerShell에서는 `.\setup.cmd`와 `.\source-ledger.cmd`를 사용합니다. macOS/Linux에서는 다음과 같습니다.

```bash
bash setup.sh
bash source-ledger.sh
```

launcher는 `http://127.0.0.1:8765`를 열고 현재 checkout 아래 `.sourceledger`를 사용합니다. 설치 방식에 맞는 명시적 명령을 사용할 수 있습니다.

```text
source-ledger ui
.\source-ledger.cmd ui
bash source-ledger.sh ui
```

사용 가능한 옵션:

```text
--workspace-root PATH   다른 로컬 워크스페이스 디렉터리를 사용합니다.
--port PORT             다른 loopback 포트를 사용합니다. 기본값은 8765입니다.
--no-browser            브라우저 탭을 열지 않고 서버만 시작합니다.
```

기본 설치만으로 UI를 사용할 수 있습니다. Playwright 수집이 필요하면 `setup.cmd --browser`, Codex·Claude 연결이 필요하면 `setup.cmd --mcp`를 실행하세요. 두 옵션을 함께 사용할 수도 있습니다. 운영체제별 내용은 [설치 안내](INSTALLATION.ko.md)를 참고하세요.

## 처음 사용

1. **Overview**에서 산업군·상품·시장을 입력합니다. 분석 목적은 묻지 않습니다.
2. **Sources**에서 모델, SKU, 카탈로그 번호처럼 정확한 상품 식별자를 입력합니다. 상품명에서 식별자를 추정하지 않습니다.
3. 명시적인 공개 URL 또는 승인된 내부 URL을 등록합니다. 등록만으로 해당 출처에 접속하지 않습니다.
4. 대기 작업을 제출하기 전에 독립 worker를 시작합니다.
5. **Runs**에서 실행 상태, 근거 상태, 관측, 누락값, XLSX 결과를 확인합니다.

첫 워크스페이스는 하나의 연구 상품 주제를 나타냅니다. 이후 정확 수집 설정에는 여러 상품을 넣을 수 있습니다.

## 화면 구성

### Overview

최초 설정 상태, 연구 진행을 막는 항목, 출처 수, 최근 작업, worker 상태를 표시합니다. worker는 웹 서버와 독립적으로 대기 작업을 실행합니다. 브라우저 탭이나 UI 서버를 닫아도 worker가 이미 맡은 작업은 취소되지 않습니다. 필요할 때 worker를 별도로 중지하세요.

### Sources

정확한 상품 식별자와 승인된 출처 후보를 관리합니다. 등록한 출처에서 같은 호스트 안의 제한된 링크 발견 또는 추출 규칙 제안을 대기열에 넣을 수 있습니다. 결과는 후보 URL 또는 제안 규칙이며 확인된 가격 관측이 아닙니다.

안내형 연구 실행은 등록된 출처 후보를 사용하며 최대 3개 출처와 고정 120초 한도로 동작합니다. UI와 로컬 assistant MCP에서는 외부 검색·모델 호출을 하지 않습니다. 검색·모델 공급자 설정은 별도 CLI 작업입니다. 없는 가격·통화·식별자·상업 조건은 빈값으로 남깁니다.

### Runs

지속 작업의 실행 상태와 근거 품질을 분리해 표시합니다.

- `queued`, `running`, `interrupted`, `failed`, `succeeded`는 프로그램 실행 상태입니다.
- 실행이 성공해도 근거 상태는 `needs_review`, `ineligible`, `partial` 등 확인되지 않은 결과일 수 있습니다.

작업 상세에서는 한도가 있는 페이지 단위 관측과 등록된 XLSX 보고서 다운로드 링크를 볼 수 있습니다. 보고서는 SQLite 원장에서 생성합니다. 출처 문장과 관측값은 신뢰할 수 없는 데이터이며 도구 지시로 취급하지 않습니다.

고급 작업에는 설정 검증, 정확 수집 실행, 지원 사이트 수집, XLSX 내보내기가 있습니다. 지원 사이트 수집은 현재 구현된 어댑터의 명시적인 페이지를 대상으로 하며 사이트 전체 또는 실시간 재고 범위의 완전성을 주장하지 않습니다.

### Connections

Codex, Claude Code, Claude Desktop에 복사할 수 있는 로컬 설정을 생성합니다. 클라이언트 설정을 직접 수정하지 않습니다. UI와 같은 워크스페이스 root를 사용하므로 최초 설정 데이터와 지속 작업 이력을 함께 볼 수 있습니다.

먼저 선택 MCP 의존성을 설치합니다.

```powershell
.\setup.cmd --mcp
```

설정 대상과 고급 CLI 연결 절차는 [로컬 AI 도구 연결](ASSISTANT_CONNECTIONS.ko.md)을 참고하세요. Codex·Claude 클라이언트 GUI는 이 브라우저 UI와 별도이며 사용할 실제 환경에서 확인해야 합니다.

## 로컬 범위

웹 서버는 `127.0.0.1`에만 연결됩니다. 이번 범위에는 인증, 원격 웹 호스팅, 제3자 MCP 클라이언트, ChatGPT 웹·모바일 직접 연결이 포함되지 않습니다.

SourceLedger는 선택한 로컬 워크스페이스 안에서 동작합니다. 연구 워크스페이스, 데이터베이스, 증거, 브라우저 프로필, 자격증명, 생성 보고서는 Git에 커밋하지 않아야 합니다. 내부 출처에는 명시적인 권한이 필요하며 로그인이나 자격증명 입력을 자동화하지 않습니다.

## CLI도 계속 사용 가능

UI는 CLI와 로컬 assistant connector가 사용하는 워크스페이스·작업 구조를 그대로 사용합니다. 고급 사용자는 다음 명령을 계속 사용할 수 있습니다.

```powershell
.\source-ledger.cmd init --lang ko
.\source-ledger.cmd research-status
.\source-ledger.cmd source-add --url "https://authorized.example/product"
.\source-ledger.cmd product-set --identifier model=EXACT-MODEL
.\source-ledger.cmd assistant start --workspace-root .sourceledger
```

전체 CLI 연구 흐름은 [시작 안내](GETTING_STARTED.ko.md), 제한된 에이전트·검증·활성화·공급자 설정은 [자동화 안내](AUTOMATION.ko.md)를 참고하세요.
