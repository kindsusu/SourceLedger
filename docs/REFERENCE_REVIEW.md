# Agent-Reach · ego-lite 코드 검토

검토 기준일: 2026-09-16. 이 문서는 로컬에 받은 소스의 HEAD를 읽어 작성했다. 패키지 설치, 전역 설정 변경, 브라우저·쿠키 접근, 외부 계정 연결 및 실제 사이트 수집은 수행하지 않았다.

| 저장소 | 검토 HEAD | 라이선스 | 이번 Windows/Python 가격수집기 판단 |
|---|---|---|---|
| `Panniantong/Agent-Reach` | `a19a171fa980a0785849596492e0af4db800c82f` | MIT | 의존성으로 설치하지 말고, 채널 라우팅·진단 설계만 재사용 |
| `citrolabs/ego-lite` | `d01be93325c7ea59d41c2ca9f4c59b58b4be4046` | MIT(저장소 코드) | Windows에서는 채택 불가. macOS 선택형 사람-브라우저 보조 경로로만 검토 |

## 결론

두 프로젝트는 가격 데이터의 출처·근거·검증·Excel 생성을 제공하지 않는다. Agent-Reach는 여러 외부 도구를 설치하고 상태를 확인하는 연결 계층이며, ego-lite는 전용 네이티브 브라우저의 JavaScript SDK다. 아래의 “재사용할 설계”는 이 코드 검토에서 얻은 **제안**이고, 현재 구현 사실은 별도 절에 분리했다.

## Agent-Reach에서 재사용할 설계

### 1. 출처 어댑터 계약과 순서 있는 대체 경로

`Channel`은 URL 처리 가능 여부, 상태 확인, 우선순위가 있는 backend 목록을 둔다. 사용자 설정이 특정 backend를 우선시하되, 알 수 없는 설정은 작동하는 backend를 가리지 않도록 무시한다. 또한 명령이 PATH에 있다는 것만으로 정상이라고 보지 않고, 가벼운 실제 probe를 하도록 요구한다. 근거: `agent_reach/channels/base.py:5-22, 29-69`.

향후 확장 시 가격수집기에 다음의 작고 명시적인 `SourceAdapter` 계약을 둘 수 있다. 이는 현재 코드에 존재하는 클래스나 API가 아니다.

```text
can_handle(source) -> bool
preflight(source) -> READY | NEEDS_BROWSER | NEEDS_AUTH | UNAVAILABLE
collect(product, source) -> EvidenceCandidate[]
```

권장 후보 순서는 `official_api → 공식 HTML/PDF → Crawl4AI 브라우저 → 사용자 확인 브라우저`다. 각 단계는 다음 단계로 넘어간 사유와 probe 결과를 남긴다. 모델이나 라우터가 가격을 채우거나, 원문이 다른 숫자를 대체하지 못하게 한다. 이 순서는 현재 실행 설정의 기본값이 아니다.

### 2. 장애가 전체 실행을 멈추지 않는 doctor

`check_all()`은 채널 하나의 예외를 `error` 상태로 낮추고 나머지 채널의 보고를 계속한다. 결과에 status, backend, 단계(tier)를 포함하고, 출력 전에 URL 자격증명을 지운다. 근거: `agent_reach/doctor.py:16-45`. 다음은 향후 가격수집기 doctor에 권장하는 범위이며, 현재 `su-crawler doctor`의 동작 설명은 뒤 절을 따른다.

- 대상 출처별 HTTP/브라우저/인증 필요 상태와 마지막 성공 시각을 기록한다.
- 상품별로 `PENDING`, `COLLECTED`, `PRICE_NOT_PUBLISHED`, `NEEDS_AUTH`, `NEEDS_HUMAN`, `FAILED`를 기록한다.
- 한 출처의 오류는 다른 출처와 Excel 생성에 영향을 주지 않는다. 최종 Excel에는 누락 원인과 재시도 예정도 함께 낸다.

### 3. web 경로의 반면교사

일반 web 채널은 URL을 `https://r.jina.ai/<URL>`로 보내어 Markdown을 받는다. 즉 로컬 브라우저가 아니라 외부 Jina Reader 서비스이며 30초 timeout과 5 MiB 제한을 둔다. Cloudflare/캡차 징후는 감지해 사이트 전용 도구 또는 브라우저로 넘긴다. 근거: `agent_reach/channels/web.py:1-66`.

이 방식은 원문 증거를 외부 서비스로 전송하고 동적 가격·옵션·로그인 세션을 보장하지 않으므로 가격의 권위 출처로 쓰지 않는다. 발견/읽기 보조로만 허용하며, 그 결과로 발견한 가격도 공식 원문 또는 브라우저 화면에서 재검증한다.

### 4. 그대로 가져오지 않을 것

- 프로젝트 스스로도 실제 읽기·검색은 upstream 도구를 직접 호출한다고 명시한다. `agent_reach/core.py:3-8, 23-28`.
- MCP 서버는 `get_status` 하나만 내놓는 stdio MCP이며, 수집 도구를 제공하지 않는다. `agent_reach/integrations/mcp_server.py:3-9, 39-75`.
- OpenCLI 기반 채널은 Chrome 확장·로컬 daemon·로그인 상태가 필요하고 Agent-Reach는 설치·health-check·routing만 한다. `agent_reach/channels/_opencli_site.py:1-46`; `agent_reach/backends/opencli.py:4-17`.
- 따라서 Agent-Reach 전체 설치나 브라우저 쿠키 가져오기 기능은 이번 수집기에 포함하지 않는다. 인증이 필요한 내부 출처는 사용자 승인을 받은 연결 방식(API, 전용 서비스 계정, 로컬 브라우저)만 별도 adapter로 두는 것을 권장한다.

## ego-lite의 실제 연동 방식

### MCP/HTTP 서버가 아니다

소스에서 발견한 연동은 SSE, WebSocket, HTTP REST, MCP transport가 아니다. `ego-browser` CLI는 stdin으로 JavaScript를 받고 `AsyncFunction`으로 실행한다. `package/ego-browser/src/run.ts:37-114`. 실행 중 SDK는 전용 앱이 주입한 `globalThis.ego` 객체를 호출한다. 런타임 여부도 `globalThis.ego.sendCDPMessage`의 존재로 판단한다. `package/ego-browser/src/browser-runtime.ts:333-344`.

CDP 요청은 JavaScript SDK가 `ego.sendCDPMessage(payload)`를 호출하고, 네이티브 앱이 `onCDPMessage(payload)` 콜백으로 응답을 돌려주는 인프로세스 브리지다. 기본 응답 timeout은 15초다. `package/ego-browser/src/browser-runtime.ts:8-12, 49-53, 359-360, 470`. 따라서 Python 수집기가 붙을 공개 endpoint, bearer token, OAuth 흐름, 또는 안정된 MCP schema는 이 저장소에서 확인되지 않았다.

### 가능한 브라우저 조작

공식 skill과 API schema는 task space 생성/선택, 페이지 이동, semantic snapshot, 클릭, 입력, 대기, CDP escape hatch, 다운로드 저장을 제공한다. 예시는 `taskSpace() → page.goto() → page.snapshot()`이며, 실제 선택자는 snapshot reference/CSS/role locator를 쓴다. 근거: `skills/ego-browser/SKILL.md:15-59`; `skills/ego-browser/references/api.md:19, 54, 71-75`; `package/ego-browser/src/public-api-schema.ts:71-72, 236, 465-507`.

가격수집 흐름에 유용한 기능은 옵션 선택 뒤 snapshot과 screenshot으로 가격·통화·수량 조건을 함께 증거화하는 것이다. 그러나 이는 별도 전용 브라우저가 실행될 때의 보조 수단이며, Python worker의 기본 driver API로 가정하면 안 된다.

### 인증과 데이터 경계

인증용 네트워크 protocol은 없고, 앱의 브라우저 profile이 가진 로그인 상태에 의존한다. README는 최초 실행 시 Chrome data migration을 선택하면 기존 로그인·쿠키·확장·북마크를 가져온다고 설명한다. `README.md:58-82`. task space는 사용자 탭과 병행하는 작업 공간이지만, 동일 browser profile을 사용할 수 있으므로 수집기에는 사내 비밀·개인 계정과 분리한 전용 profile을 요구해야 한다. 이 검토에서는 migration, login, cookie read를 실행하지 않았다.

### Windows·배포 한계

README는 ego lite가 현재 macOS에서 실행되고 Windows/Linux는 roadmap이라고 한다. `README.md:42-45`. 설치 참고문서도 install script가 macOS 전용이라고 명시한다. `skills/ego-browser/references/install.md:9-12, 64`. 패키지는 Node.js 22 이상을 요구한다. `package/ego-browser/package.json:18-21`. 또한 repository는 SDK/skill 코드이고, browser app은 별도 free download다. `README.md:225-229`.

그러므로 이 저장소의 검토 HEAD와 README 기준으로 현재 Windows 호스트에서는 설치·실행 대상이 아니다. 장래 macOS 운영자가 전용 profile과 명시적 브라우저 권한을 제공할 때, `BrowserAdapter`의 선택 구현으로만 지원하는 것을 권장한다. 앱 업데이트·native runtime 변경 때문에 SDK 직접 vendoring도 권하지 않는다.

## 현재 구현과 제안의 구분

아래는 이 작업공간의 현재 코드(`su_crawler/`)를 읽어 확인한 사실이다. Agent-Reach나 ego-lite runtime을 직접 설치·연동했다는 뜻이 아니다.

| 영역 | 현재 구현 | 근거 |
|---|---|---|
| 수집 순서 | 각 출처 설정의 `backends` 순서를 따른다. 기본값은 `http`, `playwright`이며 Crawl4AI는 선택 backend다. | `su_crawler/models.py:36`; `su_crawler/config.py:52`; `su_crawler/pipeline.py:131-171` |
| 브라우저 클릭 | `source.recipe`의 `select`, `click`, `assert_text`를 Playwright에서 실행하고 trace를 남긴다. recipe가 있으면 Playwright 상태를 유지한다. | `su_crawler/collectors.py:333-376`; `su_crawler/pipeline.py:131-133`; `tests/test_browser.py:54-81` |
| 접근 상태 | HTTP/브라우저 수집에서 로그인 화면·401은 `needs_auth`, 403/429 등 접근 제한은 `blocked`로 기록한다. | `su_crawler/collectors.py:213-226, 327-372`; `tests/test_collectors.py:85-87` |
| URL 발견 | bounded discovery는 후보 URL만 만들며 가격 근거로 취급하지 않는다. | `su_crawler/discovery.py:1-41`; `tests/test_discovery.py:1-15` |
| doctor | 로컬 HTTP·Playwright·Crawl4AI 등 backend의 설치/실행 가능 상태를 보고한다. 대상 사이트의 실제 수집 성공이나 외부 서비스 연결을 probe하지 않는다. | `su_crawler/doctor.py:1-88`; `docs/IMPLEMENTATION_STATUS.md` |

현재는 공식 API adapter, 위에서 제안한 `SourceAdapter` 클래스/계약, 출처별 마지막 성공 시각을 포함한 실수집 probe, `NEEDS_HUMAN` 상태가 구현돼 있지 않다. 또한 UWS·Jina·Exa와 Agent-Reach/ego-lite runtime은 기본 수집 경로에 연결되어 있지 않다.

## 향후 구현에 반영할 기준

1. `SourceAdapter`와 확장 Doctor는 Agent-Reach의 channel/probe 구조를 따르되, adapter가 스스로 데이터를 수집하고 증거를 반환하게 만든다.
2. browser adapter는 `BrowserObservation`만 반환한다. URL, 시각, DOM/snapshot·screenshot 경로, 클릭한 옵션, 표시 가격 문자열을 담고, `PriceValidator`가 상품/단위/통화/조건을 확인한 뒤에만 `PriceObservation`으로 승격한다.
3. 브라우저 환경이 없거나 사람이 인증·캡차를 해결해야 하면 `NEEDS_HUMAN`으로 끝낸다. 재시도 횟수와 다음 행동을 기록하며 숫자를 추정하지 않는다.
4. UWS·Jina·Exa 같은 외부 서비스는 기본 의존성이 아니다. 공개 URL을 외부로 보내도 되는 출처에 한해 사용자가 켠 adapter로 제한한다.
5. 초기 Windows 구현의 현재 HTTP→Playwright 설정 경로를 유지하면서, Crawl4AI는 설정으로 켜는 선택 backend로 둔다. ego-lite는 macOS 전용 후보로 backlog에 남긴다.

## 아직 필요한 검증 자료

- 산업군별 실제 URL 3~5개와 상품 코드·규격·수량·통화의 정답 표본.
- 인증이 필요한 출처는 허용된 접근 방식과 전용 계정/프로필의 보유 여부. 쿠키 추출은 요구하지 않는다.
- 결과 Excel에서 필요한 가격 정의(세전/세후, 배송비 포함 여부, 단가/묶음가, 유효일)와 누락 표기 규칙.
- 실제 사이트에서 현재 recipe의 옵션 선택·assert, price evidence 저장, resume 동작을 확인하는 통합 시험. 이 검토는 소스 읽기만으로 해당 동작을 보증하지 않는다.
