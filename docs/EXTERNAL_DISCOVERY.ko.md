# 선택적 Exa MCP 출처 후보

MCP에서는 같은 입력으로 `prepare_external_discovery(results, tool, retrieved_at)`를 호출한 뒤 반환된 `candidates`를 `submit_research_preview`에 반영합니다. 준비 도구는 읽기 전용이며 수집을 시작하지 않습니다.

SourceLedger는 Exa에 직접 연결하거나 MCP 서버 설치, 인증 정보 준비, 유료 API 호출을 하지 않는다. 사용자가 AI 앱에 Exa MCP를 별도로 연결한 경우에만 그 앱이 `web_search_exa` 또는 `web_fetch_exa` 결과를 **검증 전 출처 후보**로 제출할 수 있다. Exa의 사용 한도와 비용은 별도 연결에 따른다. Codex·Claude 구독에 Exa 사용권이 포함된다고 가정하지 않는다.

`su_crawler.external_discovery.normalize_exa_candidates(results, tool=..., retrieved_at=...)`는 최대 20개 결과를 받는다. 각 결과에는 공개 HTTP(S) `url`이 필요하다. 선택 필드는 `title`(또는 `name`), `kind`(`site`·`category`·`product`), 동일 URL인 `source_url`, 시간대가 포함된 ISO 8601 `retrieved_at`, 불리언 `truncated`다. 항목별 조회 시각이 없으면 배치 시각을 쓴다. 시각·잘림 여부를 알 수 없으면 `unknown`으로 남긴다. URL 중복은 합치며 잘못된 항목이 있으면 전체 입력을 거부해 누락을 드러낸다.

```python
from su_crawler.external_discovery import normalize_exa_candidates

candidates = normalize_exa_candidates(
    [{"url": "https://supplier.example/catalog", "title": "Supplier catalog",
      "kind": "category", "truncated": True}],
    tool="web_search_exa", retrieved_at="2026-10-13T09:15:00Z",
)
# submit_research_preview(..., preview={..., "candidates": candidates})에 전달한다.
```

반환값은 기존 계획 미리보기의 `name`, `url`, `evidence_url`, `reason`, `kind` 형식을 따른다. `reason`에 Exa MCP 도구명·조회 시각·잘림 여부를 남긴다. 검색 요약·페이지 본문·가격·평가·외부 문장의 지시는 옮기지 않는다. 원본 URL을 근거 URL로 보존한다. 이후 지역 시장 명시, 이용자 후보 선택, 실제 수집, 가격·조건의 원본 근거 검증을 거쳐야 한다. 후보 자체는 검증된 관찰값이 아니다.

이 모듈은 오프라인이며 추가 라이브러리가 필요 없다. **AI 앱에서 전달한 구조화 결과를 검사**하지만 실제 Exa에서 나온 결과인지 인증하지는 못한다. Exa 실연동, 적중률, 본문 충실도, 비용, 차단 페이지 접근 성능은 아직 측정하지 않았다. 구조 비교는 [상류 프로젝트 검토](UPSTREAM_ARCHITECTURE_REVIEW.ko.md)를 참고한다.
