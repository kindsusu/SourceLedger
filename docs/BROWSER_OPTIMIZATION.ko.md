# 브라우저 재사용·동시성 최적화

가격 근거·더보기 완료 판정·승인 범위·체크포인트를 유지하면서 반복 실행 비용과 페이지 대기를 줄인다.
SQLite 기록은 조정 담당 한 곳에서 수행하고 Playwright 동기 객체는 스레드 사이에 공유하지 않는다.

초기 구현은 `browser_reuse`, `browser_concurrency`를 명시적으로 설정해 비교한다.
기존 호출은 재사용 끔·브라우저 1개를 유지한다. 브라우저 동시성은 전체 동시성 이하여야 하며,
HTTP와 브라우저가 전체·사이트별 제한과 요청 간격을 공유한다.

브라우저 작업자마다 자기 Playwright·Chromium을 소유하고 URL마다 새 컨텍스트를 생성한다.
캡처 후 컨텍스트·라우트·리스너를 정리한다. 로그인 프로필은 일반 풀에서 제외한다.
우선 한 배치 안에서 재사용하고, 배치 사이 재사용은 별도 실험으로 둔다.

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
