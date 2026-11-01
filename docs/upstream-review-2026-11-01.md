# Upstream review — 1 November 2026

Selectively port the account-binding safeguards from core VaultContext commit
`4f965fed9a97fc212ad3271e96b776f8e4b4e4ca`. Google authentication records the
immutable account ID; each unlocked operation binds token use and renewal to
that original ID. ID-less cached tokens are verified by the existing refresh
endpoint before use. Replaced accounts fail before a vault request, and missing
login cannot start authentication. No cryptographic formats or key handling change.

Exclude the IdentityContext library, hosted shared login, identity assertions,
production OAuth options, deployment configuration and production release
headers/gates. The demo remains Google-only with separate enrollment, generation
checks and fixed unlock/reset deadlines. Retain the 11 October review's exclusion
of the matching-release gate: advertised `clientRevision` is installation
metadata, not server enforcement. Existing installations must update the demo
launcher and explicitly lock/unlock; this release cannot retrofit old workers.
The server pin and release ID remain unchanged.

`tests/test_account_binding.py` covers cached-account replacement, changed renewal,
ID-less token verification, missing login/no implicit authentication, a real fresh
memory worker rejecting replacement and changed demo generation, fixed worker
expiry and reset-limited unlock. Existing demo, crypto, session, OAuth and complete
recovery suites remain independent release gates. Publication and deployment
receipts are recorded separately after their checks complete.

## Local validation

On Linux, `uv sync --locked`, all 267 unit tests (16 platform/optional skips),
the nine account-binding regressions, and `uv build` passed. Using the unchanged
pinned server `416841c4d1225ddc464ce0b1e787a797d5b8d7e8`, integration,
archive migration, realtime, limits, populated backup, auth, OAuth integration,
portable client, CLI forwarding, deployment, demo backend, snapshot freshness,
signed identity rewrap, protected SQL contention and maintenance checks passed.
Standalone OAuth, deployment workflow, entrypoint, object-storage settings and
Playwright demo website checks passed with isolated synthetic fixtures.

Container image/restore gates and the published copied-launcher checks remain
release steps; these local results do not claim production deployment or live
Google-provider verification.
