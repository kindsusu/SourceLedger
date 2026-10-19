# Exa MCP·Scrapy 구조 검토

적용 현황: 제한된 병렬 수집, 간결한 MCP 진행 조회, 연결 앱의 Exa 후보 전달, 체크포인트 저장 개선을 구현했습니다. [현재 동작과 한계](CRAWLER_IMPROVEMENTS.ko.md)를 참고하세요. 아래 비교는 변경 전 구조를 검토한 기록입니다.

검토일: 2026-10-13. 두 저장소의 공개 코드와 공식 문서를 읽고 SourceLedger 현행 코드와 대조했다. 실제 Exa MCP 연결·유료 서비스 호출·Scrapy 설치·속도 실험은 하지 않았다. 상류 저장소의 `main`/`master`는 움직이는 브랜치이며, 읽기 전용 브라우저에서 커밋 SHA를 확보하지 못했으므로 구현 전에 링크의 버전을 다시 고정해야 한다.

| 구분 | 확인된 구조 | SourceLedger에 적용할 점 |
| --- | --- | --- |
| [Exa MCP 서버](https://github.com/exa-labs/exa-mcp-server) | **언어 모델이 아니라** Exa의 웹 검색·본문 조회 서비스를 AI 앱에 노출하는 MCP 서버. [`mcp-handler.ts`](https://github.com/exa-labs/exa-mcp-server/blob/main/src/mcp-handler.ts)가 [`toolRegistry.ts`](https://github.com/exa-labs/exa-mcp-server/blob/main/src/toolRegistry.ts)에 따라 도구를 등록한다. 검색은 [`webSearch.ts`](https://github.com/exa-labs/exa-mcp-server/blob/main/src/tools/webSearch.ts), 다중 URL 본문 조회는 [`webFetch.ts`](https://github.com/exa-labs/exa-mcp-server/blob/main/src/tools/webFetch.ts)에 있다. | 검색 후보 발굴·접근 실패 페이지의 선택적 대체 조회에 참고. Exa 결과 텍스트나 검색 요약만으로 가격을 검증 처리하면 안 된다. |
| [Scrapy](https://github.com/scrapy/scrapy) | **언어 모델이 아니라** Python 수집 프레임워크. [엔진](https://github.com/scrapy/scrapy/blob/master/scrapy/core/engine.py)·[스케줄러](https://github.com/scrapy/scrapy/blob/master/scrapy/core/scheduler.py)·다운로더·파서·저장 파이프라인으로 나눈다. [공식 구조 설명](https://docs.scrapy.org/en/latest/topics/architecture.html). | 큐, 중복 제거, 사이트별 동시 요청 제한, 자동 감속 설계를 차용. 전체 엔진 교체는 보류. |

Exa의 기본 도구는 `web_search_exa`, `web_fetch_exa`다. 후자는 여러 URL을 한 번에 받으며 페이지별 문자 제한 기본값은 3,000이다. 호출 횟수와 모델 입력을 줄일 수 있지만, 잘린 본문에는 가격·조건이 빠질 수 있고 DOM 맥락도 사라진다. URL별 오류를 반환하는 형태는 배울 만하다. 선택 도구 `agent_run`은 코드상 `requiresUserProvidedApiKey` 플래그로 제한된다. 호스팅 문서는 OAuth 경로도 설명하므로 이 내부 플래그를 OAuth 지원 불가로 해석하면 안 된다. 호스팅 MCP는 익명으로 시작할 수 있어도 사용량 제한이 있다. **Codex/Claude 구독이 Exa 사용료나 한도를 포함한다는 뜻은 아니다.** [Exa 저장소 설명](https://github.com/exa-labs/exa-mcp-server#authentication), [공식 문서](https://exa.ai/docs/get-started/exa-mcp).

Scrapy는 [작업 디렉터리 `JOBDIR`](https://docs.scrapy.org/en/latest/topics/jobs.html)에 미처리 요청과 방문 중복 정보를 저장해 재개한다. [요청 지문](https://docs.scrapy.org/en/latest/topics/request-response.html#request-fingerprints)은 URL만이 아니라 요청의 의미를 구분한다. [도메인별 동시 요청 제한](https://docs.scrapy.org/en/latest/topics/settings.html#concurrent-requests-per-domain)과 [AutoThrottle](https://docs.scrapy.org/en/latest/topics/autothrottle.html)는 느린 응답·오류 때 요청을 늦춘다. 다만 공식 문서는 강제 종료 때 `JOBDIR` 손상 가능성도 경고한다.

SourceLedger는 이미 [`search.py`](../su_crawler/search.py)에서 검색 결과를 후보로만 기록하고, [`catalog_collection.py`](../su_crawler/catalog_collection.py)에서 확정 계획·선택 URL·조건을 요구하며, [`pipeline.py`](../su_crawler/pipeline.py)에서 근거를 보존한다. SQLite [`catalog_checkpoint.py`](../su_crawler/catalog_checkpoint.py)는 큐·방문 URL·진행 중 URL을 저장한다. 검토한 `collect_plan` 본문은 URL을 한 페이지씩 순차 처리한다. 상위 [`assistant_runtime.py`](../su_crawler/assistant_runtime.py)의 비동기 작업 제출은 이 내부 루프의 병렬 처리를 의미하지 않는다. 다른 수집 경로의 병렬성은 이번 코드 범위에서 단정하지 않는다.

우선 수정 방향은 다음과 같다.

1. **실행 전 계획 공개:** 연결된 앱의 추천 모델(Codex Sol / Claude Opus), 실제 선택 모델, 순차·병렬 방식, 승인한 사이트·섹션, 확인된 URL 수, 시간·페이지 예산, 체크포인트 조건, 검증 기준을 사용자에게 먼저 보여준다. 각 앱의 구독 실행은 별도이며 한 앱에서 두 모델을 동시에 쓴다고 표시하면 안 된다.
2. **지속 가능한 제한 병렬 큐:** 승인 URL마다 상태(`queued`·`leased`·`completed`·`review`·`failed`), 시도 횟수·기한·출처를 저장한다. 요청 식별자에는 결과를 바꾸는 섹션·쿼리 상태를 포함한다. 사이트별 동시 수를 작게 시작하고 429/5xx에는 감속한다. 브라우저 수집은 별도 낮은 한도를 둔다. 완료 저장은 중복 실행에 안전하게 한다.
3. **간결한 MCP 결과:** 한 번 시작한 작업은 상태·요약·페이지별 결과로 조회한다. 모델에 전체 HTML이나 수백 건의 근거를 매번 되돌리지 않는다. URL별 영수증과 상세 근거는 필요할 때 펼친다.
4. **Exa는 선택적 실험:** 이용자가 명시적으로 활성화한 뒤 승인된 표본에서 검색 적중, 차단 페이지 접근, 비용·한도, 필드·근거 일치율을 측정한다. 기본 경로와 오프라인 테스트는 유지한다.

병렬 구현의 완료 조건은 고정된 동일 URL·섹션 목록, 체크포인트 켜짐, 중단·재개 후 누락과 중복 없음, 승인 범위 준수, 가격·거래 조건의 일치 또는 차이 명시, 근거 해시 보존, 반복 실행 시간 비교다. AI 앱의 요청부터 최종 정리까지 소요 시간과 토큰은 별도 계측해야 한다. 이번 검토만으로 개선 속도나 Exa의 차단 우회 성공을 주장할 수 없다.
