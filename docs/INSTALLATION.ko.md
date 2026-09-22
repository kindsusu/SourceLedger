# SourceLedger 설치

[English](INSTALLATION.md) | [한국어](INSTALLATION.ko.md)

SourceLedger는 Python 3.11 이상을 지원하며 Python 3.12를 권장합니다. CI는 Python 3.11부터 3.13을 검사하도록 구성되어 있습니다. 로컬 core 수집에는 호스트 AI 도구, 유료 MCP 서비스, Claude/ChatGPT 클라이언트 연동이 필요하지 않습니다.

## 프로젝트 받기

저장소를 clone하거나 GitHub의 **Code → Download ZIP**으로 내려받아 압축을 풉니다. clone하는 경우 다음을 실행합니다.

```bash
git clone https://github.com/kindsusu/SourceLedger.git
cd SourceLedger
```

아래 명령은 압축을 푼 폴더 또는 clone한 프로젝트 폴더에서 실행합니다.

## Windows

명령 프롬프트를 열고 실행합니다.

```bat
setup.cmd --browser
source-ledger.cmd init
source-ledger.cmd research-status
```

기본 `setup.cmd`는 core만 설치합니다. 필요한 기능만 옵션으로 추가합니다.

```bat
setup.cmd --browser
setup.cmd --mcp
setup.cmd --dev
```

`--browser`는 선택 Playwright 패키지와 Chromium을 설치합니다. `--mcp`는 로컬 MCP 의존성을, `--dev`는 테스트 의존성을 설치합니다. launcher는 위치 기준으로 프로젝트의 `.venv`를 사용하므로 `Activate.ps1` 실행이나 PowerShell 실행 정책 변경이 필요 없습니다. CLI 인자는 그대로 전달되며, 한국어 첫 설정은 다음처럼 실행합니다.

```bat
source-ledger.cmd init --lang ko
```

인자 없이 `source-ledger.cmd`를 실행하면 기본 워크스페이스가 없을 때는 `init`을, 초기화 뒤에는 `research-status`를 실행합니다.

브라우저 수집을 창 없이 실행하려면 현재 셸에서 `SOURCELEDGER_HEADLESS=1`을 설정합니다.

```bat
set SOURCELEDGER_HEADLESS=1
source-ledger.cmd collect-sites --url "https://www.jetcar.kr/sub0201/<vehicle-id>"
```

PowerShell에서는 launcher 명령 전 `$env:SOURCELEDGER_HEADLESS = "1"`을 사용합니다.

## macOS와 Linux

터미널에서 실행합니다.

```bash
bash setup.sh --browser
bash source-ledger.sh init
bash source-ledger.sh research-status
```

인자 없이 `bash source-ledger.sh`를 실행하면 기본 워크스페이스가 없을 때는 `init`을, 초기화 뒤에는 `research-status`를 실행합니다.

옵션의 의미는 Windows와 같습니다.

```bash
bash setup.sh --browser
bash setup.sh --mcp
bash setup.sh --dev
```

Linux에서는 `--browser`가 일반 사용자로 managed Chromium을 내려받습니다. 브라우저 실행에는 운영체제 패키지가 추가로 필요할 수 있습니다. 일반 사용자 설치를 마친 뒤 프로젝트 가상환경으로 시스템 의존성만 설치합니다.

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

이 명령은 Linux 패키지를 설치하므로 관리자 승인이 필요한 시스템 변경입니다. 지원 Linux 배포판과 의존성 동작은 Playwright의 [브라우저 설치 안내](https://playwright.dev/python/docs/browsers)를 확인하세요. headless 브라우저 수집은 `SOURCELEDGER_HEADLESS=1 bash source-ledger.sh collect-sites --url "https://www.jetcar.kr/sub0201/<vehicle-id>"`처럼 실행합니다.

## 첫 실행과 다른 컴퓨터로 이동

새 컴퓨터마다 `init`을 실행해 산업군·상품·시장을 입력한 뒤 출처를 등록하세요. 새 폴더나 새 컴퓨터에서는 새 `.venv`를 설치해야 하며, 기존 가상환경을 복사하지 마세요.

활성 설정과 SQLite 근거 이력에는 절대 evidence 경로가 들어갈 수 있습니다. 따라서 기존 연구 워크스페이스만 복사해도 활성 설정이나 보고서 이력이 이식된다고 보장하지 않습니다. 새 컴퓨터에서 새 워크스페이스를 만들고, 승인된 출처와 규칙을 다시 등록하세요. 브라우저 profile, 쿠키, 자격증명 등 개인 profile 데이터는 컴퓨터 간에 복사하지 마세요.

로컬 MCP 서버는 선택 기능입니다. Claude와 ChatGPT 클라이언트 직접 연동은 검증되지 않았습니다.

## 테스트 실행

전체 오프라인 테스트는 선택 MCP와 브라우저 경로를 import하므로 모든 테스트 extra를 먼저 설치합니다.

```bash
bash setup.sh --dev --mcp --browser
.venv/bin/python -m pytest tests -q
```

Windows에서는 다음을 사용합니다.

```bat
setup.cmd --dev --mcp --browser
.venv\Scripts\python.exe -m pytest tests -q
```

`pyproject.toml`은 지원 의존성 범위를 선언하고, `requirements/constraints.txt`는 완전한 transitive lockfile이 아닌 직접 의존성 설치 기준을 고정합니다.
