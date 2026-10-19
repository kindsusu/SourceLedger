# Diagram provenance

This directory contains public documentation of SourceLedger, not collected source material or operational reports.

- **SourceLedger code snapshot:** [`957b5f1f96e5d3584e2726a07fe91e421947e83d`](https://github.com/kindsusu/SourceLedger/commit/957b5f1f96e5d3584e2726a07fe91e421947e83d), version 0.3.0.
- **Diagram authoring tool:** [Archify](https://github.com/tt-a1i/archify), version `2.17.0-dev.1`, pinned to [`72c750bb070d95171dbb2244e5b62b1b7da69c12`](https://github.com/tt-a1i/archify/tree/72c750bb070d95171dbb2244e5b62b1b7da69c12/archify).
- **Installed package:** the upstream `archify/` directory, installed as a local Codex skill. The SourceLedger repository contains the authored JSON and rendered documentation; it does not vendor or automatically install the skill.
- **Runtime used for rendering:** Node.js `v24.18.0` on Windows.
- **Language:** English diagram and viewer, with English and Korean explanatory guides.

The diagram's components and relationships were checked against the Python code linked in [the architecture guide](../ARCHITECTURE.md). Archify checks schema and diagram geometry; those checks do not establish that a system description is factually correct, measure collection accuracy, or test external providers.

The generated HTML includes Archify's viewer code and embedded fonts. Their upstream licenses are preserved in [LICENSE.archify.txt](LICENSE.archify.txt), [JetBrainsMono-OFL.txt](JetBrainsMono-OFL.txt), and [THIRD_PARTY_NOTICES.archify.md](THIRD_PARTY_NOTICES.archify.md). The diagram does not request third-party brand marks.

The [verification record](verification.json) binds the published JSON and HTML bytes to the diagram and browser checks. Local raw receipts and temporary browser captures are not repository artifacts. After any diagram change, regenerate the HTML and preview and update the verification record after inspecting the new output.

한국어: 이 폴더는 SourceLedger 공개 코드의 동작을 설명하는 문서입니다. 수집 원문이나 운영 보고서는 포함하지 않습니다. Archify의 형식·배치 검사와 실제 코드의 사실 확인은 별개이며, 외부 서비스나 수집 정확도를 검증했다는 의미가 아닙니다. 원본 스킬의 버전·커밋과 라이선스를 기록하고, 생성한 JSON·HTML은 검증 기록의 해시로 연결합니다.
