# 브라우저 재사용·동시성 최적화

가격 근거·더보기 완료 판정·승인 범위·체크포인트를 유지하면서 반복 실행 비용과 페이지 대기를 줄인다.
SQLite 기록은 조정 담당 한 곳에서 수행하고 Playwright 동기 객체는 스레드 사이에 공유하지 않는다.

새 조사 계획 작업의 기본값은 `browser_reuse=true`, `browser_concurrency=1`이다.
이 기본값 이전에 대기열에 들어간 작업은 재개할 때 저장된 설정을 유지한다. 브라우저 동시성은
전체 동시성 이하여야 하며, HTTP와 브라우저가 전체·사이트별 제한과 요청 간격을 공유한다.

페이지 작업자(`concurrency`)가 HTTP 수집과 추출을 맡고, 브라우저 캡처는 `browser_concurrency`개의
전용 브라우저 스레드에서 실행한다. 브라우저 스레드마다 Playwright와 Chromium 하나를 소유하므로
`browser_concurrency=1`이면 Chromium 하나만 시작한다. 순차 경로도 자기 스레드에서 Chromium 하나를
재사용한다. URL마다 새 컨텍스트를 만들고 캡처 후 컨텍스트·라우트·리스너를 정리한다. 로그인 프로필은
제외한다. 풀 수명은 한 배치이며, robots 판단과 HTTP 연결은 `collection_job_scope()`로 배치 사이에
공유할 수 있다.

## 수집 순서와 대기

- 렌더링 캡처로만 구간이 완료되는 어댑터는 카탈로그 수집에서 브라우저를 먼저 실행하고, 브라우저가
  실패·시간 초과·사용 불가이거나 항목을 추출하지 못할 때만 HTTP를 대체 수집한다. 다른 어댑터는
  HTTP를 먼저 수집하고, 모든 구간이 완료이며 근거가 충분할 때만 거기서 멈춘다(정적 HTML은 충분하지
  않다). 정적 상세 페이지 어댑터는 아래의 예외를 따른다. `collect_sites`는 모든 응답에서 상품을 만들기 때문에 두 수집
  방식을 모두 유지한다.
- 목록 펼치기와 열거는 최종 캡처 예비 시간(최대 15초)을 남긴다. 최종 캡처가 안정되지 않으면 렌더링
  DOM을 보존하되 구간은 부분(`observation_unsettled`)으로, 행은 검토 대상으로 기록한다.
- 목록 끝, 다음 페이지 버튼의 부재·비활성, "더보기" 버튼 부재는 추적 중인 fetch/XHR·로딩 표시가
  없는 안정 구간을 확인해야 완료로 본다. 모든 추가 로드가 네트워크로 확인된 목록은 전체 진행 대기
  대신 2.5초의 조용한 구간으로 끝을 확인할 수 있다. 네트워크 추적이 없는 페이지는 기존 고정 대기를
  유지한다.
- 랜딩 페이지의 선택적 프로모션 팝업 대기는 바꾸지 않았다. 팝업이 나타나지 않음을 확정할 수
  없고, 늦게 뜬 팝업이 가격표 클릭을 막을 수 있기 때문이다.

## 정적 상세 페이지: HTTP 채택과 표본 브라우저 검증

일부 어댑터의 상세 페이지(현재 `jetcar` 어댑터)는 렌더링 화면과 같은 렌트 표를 정적 HTML에도 담고 있으며, 이런 페이지가 많을 때는
브라우저 단계(이동·안정화·증거 캡처)가 수집 시간의 대부분을 차지한다. 이 방식을 켜기 전에 전체 목록에서
보존된 HTTP 추출 결과(체크포인트 페이지 기록의 `raw_http_candidates`)와 채택된 브라우저 행을 비교해,
차이가 더 이상 존재하지 않는 매물에 한정되는지 확인한다. 이후에는 표본 검증이 작업마다 이를 계속 확인한다.

그래서 카탈로그 수집(`collect_plan`, 조사 계획·배치 작업)은 이런 상세 페이지를 다음처럼 처리한다.

1. HTTP를 먼저 수집한다. 수집에 성공하고 행을 추출했으며 어댑터 구간이 모두 완료(어댑터의 기존 구간
   판정)이면 HTTP 결과를 채택하고 브라우저를 건너뛴다. 페이지 기록에
   `{"event": "browser_skipped", "reason": "http_complete_jetcar"}`를 남긴다.
2. 검증 표본은 계속 브라우저로 수집한다. 작업에서 해당 어댑터 URL 중 `browser_sample_ratio`(기본 0.1), 최소
   5개(5개 미만이면 전부)를 작업 식별자와 URL의 안정 해시로 고른다. 같은 작업에서는 재현되고 작업마다
   바뀌며 배치 전체에 퍼진다. 표본 페이지는 기존처럼 렌더링 캡처를 증거로 채택하고, HTTP와 브라우저의
   행 다중집합을 `price`, `currency`, `price_basis`, `term_months`, `deposit_amount`,
   `annual_mileage_km`, `name`, `odometer_km`, `availability`로 비교한다. 결과는
   `browser_sample_verification`(`match`·`mismatch`·`unverified`와 행 수)으로 기록한다.
3. 하나라도 불일치하면 그 작업에서 해당 어댑터의 남은 페이지는 이후 배치까지 모두 브라우저로 수집한다
   (`browser_required`, `jetcar_sample_mismatch_fallback`). 재개한 작업은 작업 기록·체크포인트·재생된
   배치 결과에 남은 전환 상태를 유지하고, 기록이 없으면 남은 페이지에서 다시 표본을 뽑는다. 기록되지
   않은 통과를 가정하지 않는다.
4. HTTP 실패·시간 초과·구간 부분/미확인·행 없음은 기존처럼 브라우저를 사용한다. 표본 페이지의 브라우저
   실패는 HTTP 결과를 유지하고 `unverified`로 센다. 통과도 불일치도 아니다.

증거 측면의 대가: 채택한 행은 정적 HTML이다. `evidence_mode=static_html`, `source_visibility=unconfirmed`를
유지하므로 검토 대상으로 남고, 렌더링으로 확인된 표시 가격처럼 비교되지 않는다. 이전에는 같은 페이지가
렌더링 증거를 만들었다. 표본은 표본 밖 페이지가 렌더링 화면과 다를 위험을 줄일 뿐 없애지는 못한다.
`concurrency > 1`이면 불일치를 발견한 시점에 이미 처리 중인 페이지는 채택한 HTTP 결과를 유지한다.

`browser_sample_ratio`는 0보다 크고 1 이하여야 하며 `1.0`이면 기존처럼 항상 브라우저를 사용한다.
`collect_plan` 인자, 조사 배치 작업 인자(`start_research_batch_run`), `collect-plan`의
`--browser-sample-ratio` 옵션으로 지정한다. 실행 `execution` 기록에 `jetcar_http_accepted`,
`jetcar_browser_sampled`, `jetcar_sample_mismatches`, `jetcar_sample_unverified`,
`jetcar_browser_fallback`를 남기고, 배치 진행 상황과 수집 요약은 이를 `jetcar_sampling`으로 합산한다.
브라우저 우선 어댑터는 계속 브라우저를 먼저 쓰고, 다른 어댑터는 바뀌지 않으며, `collect_sites`는 두 수집 방식을 모두
유지한다.

## 외부 코드 검토

- [Crawlee BrowserPool](https://github.com/apify/crawlee-python/blob/master/src/crawlee/browsers/_browser_pool.py): 여유 브라우저 재사용, 수명 관리와 종료.
- [scrapy-playwright](https://github.com/scrapy-plugins/scrapy-playwright/blob/main/scrapy_playwright/handler.py): 페이지·컨텍스트 제한, 시작 잠금, 실패 시 자원 반환.
- [Crawl4AI](https://github.com/unclecode/crawl4ai/blob/main/crawl4ai/async_dispatcher.py): 메모리 기반 작업 제한과 사이트별 재시도 대기. 메모리 자동 조절은 후속 후보이다.
- [Scrapy](https://github.com/scrapy/scrapy/blob/master/scrapy/extensions/throttle.py): 빠른 오류 응답으로 가속하지 않는 지연 기반 속도 조절. 후속 조정 후보이다.
- [ego-lite](https://github.com/citrolabs/ego-lite): 작업별 탭 관리와 사용자에게 제어 전달. 공개 도우미는 자체 브라우저 런타임에 의존하며, 검토 시점 README는 macOS 지원·Windows 비공개 베타 예정으로 안내한다. 이번 성능 비교에 도입하지 않는다.

전체 프레임워크를 교체하지 않고 필요한 설계만 참고한다. 유료 서비스나 모델 API를 추가하지 않는다.

## 검증 순서

승인된 동일 URL 목록에서 브라우저 재사용 전후와 제한된 동시성을 비교합니다. 배치 크기, 체크포인트, 증거 조건을 고정하고 요청부터 답변까지의 시간과 수집 시간, 브라우저 시작 횟수, 오류, 범위·가격·조건·해시를 확인합니다. 스케줄링 변경 효과는 재사용 효과와 구분합니다. 실제 조사 결과는 Git 제외 로컬 작업 공간에 보관합니다.

## 결과 해석

브라우저 재사용과 제한된 동시성은 구현된 선택 기능입니다. 속도 효과는 페이지·사이트 제한·실행 환경에 따라 달라집니다. 표본 결과만으로 전체 목록 완전성이나 보편적인 모델 순위를 주장할 수 없습니다. 같은 조건으로 비교하고 사이트별 측정값은 로컬 조사 기록에 남깁니다.
