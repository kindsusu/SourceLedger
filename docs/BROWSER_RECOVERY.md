# Browser challenge recovery

[한국어](BROWSER_RECOVERY.ko.md)

SourceLedger detects recognizable access challenges and prepares **connected-AI browser assistance**. It does not contain a CAPTCHA solver, invoke a paid service, start an AI conversation, or transfer the AI app's cookies. Actual interaction depends on the connected application's available tools and policies. This feature does not guarantee challenge completion.

## Workflow

1. Collection records `blocked` and an `access_challenge` trace. Recognized challenges do not trigger automatic backend fallback or transient retries. Earlier safe captures remain evidence; challenge contents never become prices. Other collection targets continue under the existing job limits.
2. The UI's **Prepare browser assistance** button displays copyable instructions for the affected issue. Preparing instructions spends no attempts and does not start the AI app. From an MCP-connected AI app, call `prepare_browser_recovery(job_id, issue_id)` directly.
3. If that app has a browser and supports the interaction, call `begin_browser_recovery` with both `browser_available` and `challenge_interaction_supported` true. Then perform one bounded attempt in the approved page's tab. Otherwise request user assistance directly.
4. Call `finish_browser_recovery` with the returned `attempt_id` and `blocked`, `needs_user`, or `accessible`. Accessible requires the exact approved URL and a literal excerpt of the target content. A checkbox or disappearance of a dialog alone is insufficient. Never submit credentials.
5. Continue evidence collection in that same host tab after successful access. Single research jobs can use `submit_browser_evidence`; batch jobs need a reviewed follow-up research plan for that submission path. Host evidence and access reports remain unverified, and issue resolution still requires the normal revalidation evidence.

Two reservations are allowed per source run and exact URL; state survives restarts and repeated preparation. Repeating an unfinished reservation returns the same attempt ID. Finished results cannot be overwritten. `needs_user` and reported access prevent further automatic reservations in that run. An interrupted attempt remains reserved until its outcome is recorded. Starting a new run is not permission to bypass the budget by repeatedly attempting challenges.

The server does not inherit the host tab's session. Server recollection with an existing authorized local profile requires a reviewed plan rule and confirmation. There is no one-click transfer from a Codex/Claude browser into Playwright. The UI does not mark access restored or resolve the issue merely because instructions were prepared.

## Validation and limits

Offline fixtures exercise HTTP 200 challenge detection, ordinary product/contact-form false positives, HTTP blocking, prior-capture retention, durable attempt limits, MCP calls and the protected local Web API. Recognition is intentionally conservative and does not cover every provider, custom widget, or JavaScript-only state. Unknown access failures remain issues for inspection.

Earlier offline validation on 2026-10-14: the affected integration suite had 99 passes and one clipboard-mock test failure. After correcting the mock, all 5 UI tests passed. Final detection/recovery tests passed 29 cases, including a localhost Playwright page that changes from a collected option to a challenge. Test sets overlap; they are not an aggregate count. Python compilation and Git whitespace checks passed.

Live validation on 2026-10-14 used the connected Codex browser. On Google's official reCAPTCHA demo, one approved checkbox attempt showed a checkmark, and normal form submission returned a server success message; no image puzzle appeared. Jetcar and Gongcar product pages presented no CAPTCHA. SourceLedger HTTP collection of the Google demo stopped at `robots_disallowed`, without overriding the site's rule. Local stdio MCP `initialize`/`list_tools` exposed the three recovery tools, but no recovery calls were made. No paid API was used.

No genuine blocked product-site issue occurred, so the full recovery flow, same-tab evidence submission, and access restoration remain unvalidated. Image and audio challenges, other providers, and Claude browser hosting were not tested. Passing the demo does not establish that SourceLedger can recover a blocked source.
