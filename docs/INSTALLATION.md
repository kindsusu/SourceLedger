# Install SourceLedger

[English](INSTALLATION.md) | [한국어](INSTALLATION.ko.md)

SourceLedger supports Python 3.11 or later; Python 3.12 is recommended. CI is configured to check Python 3.11 through 3.13. Core local collection does not require a host AI tool, a paid MCP service, or a Claude/ChatGPT client integration.

## Get the project

Clone the repository, or use **Code → Download ZIP** on GitHub and extract the archive. For a clone:

```bash
git clone https://github.com/kindsusu/SourceLedger.git
cd SourceLedger
```

Run the following commands from the extracted or cloned project directory.

## Windows

Open Command Prompt and run:

```bat
setup.cmd --browser
source-ledger.cmd init
source-ledger.cmd research-status
```

The default `setup.cmd` installation is core-only. Add options when needed:

```bat
setup.cmd --browser
setup.cmd --mcp
setup.cmd --dev
```

`--browser` installs the optional Playwright package and downloads Chromium. `--mcp` installs the local MCP dependency. `--dev` installs the test dependency. The launcher uses the repository's `.venv` by location, so it does not require `Activate.ps1` or a PowerShell execution-policy change. CLI arguments pass through unchanged; for Korean first-run prompts use:

```bat
source-ledger.cmd init --lang ko
```

With no arguments, `source-ledger.cmd` starts `init` when no default workspace exists and `research-status` after initialization.

For browser collection without a visible window, set `SOURCELEDGER_HEADLESS=1` in the current shell:

```bat
set SOURCELEDGER_HEADLESS=1
source-ledger.cmd collect-sites --url "https://www.jetcar.kr/sub0201/<vehicle-id>"
```

In PowerShell, use `$env:SOURCELEDGER_HEADLESS = "1"` before the launcher command.

## macOS and Linux

In a terminal, run:

```bash
bash setup.sh --browser
bash source-ledger.sh init
bash source-ledger.sh research-status
```

With no arguments, `bash source-ledger.sh` starts `init` when no default workspace exists and `research-status` after initialization.

The options have the same meaning as on Windows:

```bash
bash setup.sh --browser
bash setup.sh --mcp
bash setup.sh --dev
```

On Linux, `--browser` downloads managed Chromium as the normal user. Browser use can also need operating-system packages. After that normal-user setup, install only the system dependencies with the project virtual environment:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

The command can install Linux packages and needs an administrator-approved system package operation. Playwright's supported Linux distributions and dependency behavior are documented in its [browser installation guide](https://playwright.dev/python/docs/browsers). For headless browser collection, run `SOURCELEDGER_HEADLESS=1 bash source-ledger.sh collect-sites --url "https://www.jetcar.kr/sub0201/<vehicle-id>"`.

## First run and moving computers

Run `init` on each new computer and enter the industry, product, and market before adding sources. A new folder or computer needs a new `.venv`; do not copy one between machines.

Activation configurations and SQLite evidence history can contain absolute evidence paths. Copying an existing research workspace alone therefore does not guarantee a portable active configuration or report history. Create a new workspace on the new computer, then re-register authorized sources and rules as needed. Do not copy browser profiles, cookies, credentials, or other private profile data between computers.

The local MCP server is optional. Direct Claude and ChatGPT client integrations have not been verified.

## Run tests

The full offline suite imports optional MCP and browser paths, so install all test extras first:

```bash
bash setup.sh --dev --mcp --browser
.venv/bin/python -m pytest tests -q
```

On Windows, use:

```bat
setup.cmd --dev --mcp --browser
.venv\Scripts\python.exe -m pytest tests -q
```

`pyproject.toml` declares supported dependency ranges; `requirements/constraints.txt` pins the direct installation baseline without being a complete transitive lockfile.
