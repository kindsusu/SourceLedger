# CI and release checks

The `Tests` workflow runs on pushes and pull requests. Its matrix covers Ubuntu with Python 3.11, 3.12, and 3.13, plus Windows and macOS with Python 3.12. A separate job builds and installs the wheel. **All environments passed** succeeds only when the full matrix and package job succeed; a cancelled or skipped dependency also fails the gate.

Each matrix job saves JUnit output and runtime/package versions as a short-lived `test-diagnostics-*` artifact. Browser tests that opt into the shared diagnostic fixture add a screenshot and Playwright trace when they fail. The artifacts are for test fixtures only; do not run collection against private research sources in CI or upload research workspaces, profiles, cookies, credentials, or collected documents.

Before declaring a commit complete, inspect the `Tests` run for that exact commit and wait for **All environments passed**. A local suite passing is insufficient when the failure is platform dependent.

To publish a version, push the version commit to `main` and push its existing version tag, then wait for the commit's push-triggered `Tests` run to pass. Manually dispatch **Verified release** with that tag. The release workflow resolves the tag to an exact commit and checks the **latest** `main` push-triggered `Tests` run for that commit. An in-progress, cancelled, or failed newer run blocks release even if an older run passed. The latest run must succeed with the aggregate gate. The workflow downloads distributions from that same run, checks their filenames against the tag version, and publishes the release. It does not rebuild at release time or publish on ordinary pushes.

The workflow protects its own release path. Repository administrators can still create releases outside this workflow. The repository's `main` branch currently has no branch protection (GitHub API returned 404); configure **All environments passed** as a required check and restrict release permissions separately if all merge and release paths must be governed.
