# Demo implementation validation — 9 October 2026

This file preserves historical validation. The final **Root-path / skill-only
release** section supersedes all hosted-wheel, download-readiness and fallback
installation descriptions below. Current installation uses the public Git-backed
skill only; see the README.

## Google popup correction — local validation, 9 October 2026

The live Chrome flow returned “Google sign-in was closed” after Google consent.
The served page uses `Cross-Origin-Opener-Policy: same-origin`. A two-origin
loopback Chrome reproduction confirmed that an open cross-origin popup can
report `closed=true` to its parent under that policy. The old 700 ms poll
therefore cannot distinguish cancellation from browser isolation or a successful
redirect closing before its separate realtime callback arrives.

The correction removes that poll, retains the security header, state/PKCE checks
and three-minute timeout, and adds an explicit Cancel Google sign-in button.
Cancelled attempts ignore late callbacks and token-exchange results.

Local checks: 81 demo unit checks ran, with 76 passing and five skips requiring
external test binaries/platform support. The new OAuth ordering regression fails
against the original code and passes against the correction. It covers a closed
handle before callback, duplicate callback, explicit cancellation before/during
exchange, timeout, state mismatch, provider rejection and connection failure.
Chrome desktop/mobile synthetic UI checks passed delayed-callback sign-in,
keyboard cancellation, unchanged optional choices and no horizontal overflow.
JavaScript syntax, asset checksum and Git whitespace checks passed.

The existing offline browser suite also models a closed handle and delayed
callback now; that full suite and image CI have not been rerun for this patch.
The correction is not yet deployed, and successful real Google sign-in and
enrollment remain unverified.

## Previous implementation baseline

This records local synthetic validation of the `vaultcontext-demo` worktree. It is not evidence of a public deployment or a completed provider migration. The source repository was private for these initial checks; it is now public. Public client artifacts are generated during the image build; generated wheels and bundles are not committed. Repeat builds with the tested tooling produced identical artifacts. The build backend is pinned, but its transitive build-tool dependencies are not fully locked; content-addressed paths prevent changed bytes from silently replacing a prior release.

Final local regression: 176 unit checks ran successfully (169 passed, 7 platform-specific skips). This includes the real pinned-server and Litestream cases enabled through their test environment variables. The 43 production entrypoint checks and 10 deployment-workflow checks also passed, as did demo backend, signed identity, deployment/object-storage settings, browser onboarding, and the two public-launcher integration suites.

## Recovery and isolation

- Exact pinned PocketContext server revision `976ddf71a4734530adefe4a56633658a0894b449`, built with CGO and the declared SQLite tags.
- Exact image-pinned Litestream 0.5.17 Linux ARM64 binary, verified against the Dockerfile SHA-256 before execution.
- Actual Litestream file-replica round trips for populated main and auxiliary databases; original local files removed before entrypoint recovery.
- Actual paired handoff uses a forced snapshot, independently restores both databases, compares logical digests, and adopts them into another isolated directory. The original UTC generation is preserved.
- Actual contact-service enrollment, security event, withdrawal, write draining, final synchronization, replica restoration and handoff digest checks. Unverified disaster recovery disables restored marketing permissions.
- Synthetic reset tests use isolated databases, mocked provider operations and a real pinned-server local lifecycle. Contacts survive the full disposable runtime purge.
- Docker lifecycle, credential separation, source/target fencing and failure handling have command/metadata tests. These are not a substitute for running the containers.

## Client and application

- Public wheel and standalone launcher served from an isolated loopback HTTP origin. Artifact sizes and SHA-256 hashes checked before installation. `uv tool install` and copied launcher execution use no private GitHub dependency; GitHub credential environment variables were excluded from the installation check.
- Full portable client and copied CLI exercises pass against the downloaded public-wheel launcher: exact original bytes, immutable versions, comparison privacy, protected-file authorization, revocation, literal paths, archive handling, request limits and independent memory-session operation.
- Demo backend validation covers verified synthetic Google admission, required terms, optional separate purpose preferences, private directory/metadata reads, quotas, disabled alternative authentication and daily-generation expiry.
- Signed identity replacement rejects token-only, wrong-signature, tampered and stale requests, while preserving the encrypted bundle and actor-private audit behavior.
- Existing deployment settings, object-storage configuration and production entrypoint checks run alongside the demo checks. Browser tests cover desktop/mobile preview, onboarding, consent conflicts, expiry and unsubscribe behavior.

## Remaining release checks

A Docker daemon is unavailable on this local host. Image configuration, smoke and provider-backed populated restore/migration checks must pass in container CI and a dedicated staging environment. CI invokes the real replication tests using the Litestream binary extracted from the built image.

Before public enrollment, finalize legal identity/contact details, confirm the disclosed backup-expiry policy with actual provider cleanup behavior, configure the separate Google OAuth client, validate live personal/Workspace login, provision the isolated storage and stable HTTPS origin, and exercise the complete daily reset and host cutover. Verify source-host timers/deployment controllers remain fenced and old disposable local copies are removed before midnight. No production deployment, provider resource creation, external commercial message or newsletter subscription was performed by these local checks.

## Public skill installation (2026-10-09)

Anonymous `npx skills add` from the explicit `vaultcontext-demo` skill URL was
verified in an isolated workspace with empty Git configuration and credentials.
The revised local skill was separately installed in an isolated workspace; its
pinned client fetched anonymously and both general and Google-login help worked.
The site retains verified-download readiness and the wheel installation fallback.

A bounded review of 82 locally reachable commits / 489 unique blobs found no
confirmed credentials. Pattern matches were synthetic loopback test expressions.
This was not a comprehensive secret audit: deployment/credential paths,
untracked files, remote-only history and organization settings were excluded.
PR workflows have read-only permissions and do not publish or deploy. No
project-level license has been selected; vendor licenses remain unchanged.

## Root-path / skill-only release

This release supersedes the hosted-wheel and archive installation evidence above.
The demo is served at `/` with root legal pages. Old `/demo` and download routes
return 404, including direct old asset URLs. Installation is advertised through
the public skill source and a client revision derived from the packaged launcher.
No hosted client artifacts are built or served. API access control and the
three-database reset/recovery boundaries are unchanged.
