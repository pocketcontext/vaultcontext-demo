# Audit follow-up — 11 October 2026

This source review and remediation follows workspace audit
`AUDIT-vaultcontext-demo-2026-10-11.md`, against demo base `194f724`.
The original audit remains historical evidence; this document records the
subsequent changes and their validation. No live application data was used.

## Disposition

1. **Core review:** recorded selective adoption and explicit exclusions in the
   [upstream review](upstream-review-2026-10-11.md). The actor-only audit exception
   now admits only identity-rewrap events. The demo keeps its existing client
   installation contract instead of importing core's API-wide release gate.
2. **SQL contention:** reproduced cross-user denial with synthetic enrolled
   visitors, then enabled one admitted SQL request per account before the two
   shared slots. The unaffected visitor's query improved from HTTP 408 after
   five seconds to HTTP 200 in 10 ms; three excess same-account requests received
   prompt HTTP 429. See [measurements](sql-contention-2026-10-11.md).
3. **Account exhaustion:** retain the 1,000-account daily resource cap and record
   distributed enrollment as residual availability risk. More accounts or IPs
   can still exhaust a finite public service; increasing the cap is not a fix.
4. **Enrollment consistency:** commit the contact preference decision and its
   bounded `enrollment_request` observation together. The event does not claim
   vault admission. A later application failure retains the contact decision;
   admission remains closed until its separate transaction commits. Stale retries
   conflict and require refreshed preferences and another explicit submission.
5. **Withdrawal validation:** both endpoints accept only 32–128 ASCII URL-safe
   token characters. Invalid length, Unicode, whitespace and punctuation return
   validation errors rather than a misleading service-unavailable response.
6. **Withdrawal capabilities:** deliberately retain old links for the contact's
   lifetime. Expiry/deletion removes their hashes. Rotating away old links would
   break withdrawal from earlier correspondence; no retention policy changed.
7. **Deployment evidence:** live `/up` must report the triggering `GITHUB_SHA`,
   in addition to matching assets, installation metadata and current generation.
   Regression checks reject a coherent but wrong release.
8. **Cleanup:** correct the image title and redundant reset-marker write. Reset
   selects the R2 adapter only for exact Cloudflare endpoints and uses version-aware
   S3 deletion elsewhere. Historical wheel evidence is explicitly marked superseded;
   repository-separation documentation now records core's security backports.

## Initial local validation scope

Tests use temporary synthetic databases, accounts and files. Real Google browser
sign-in, cloud-provider retention behavior and deployed revision were not checked.
No changes have been deployed. The account cap and multi-account resource
exhaustion remain residual risks; per-account admission is not Sybil resistance.

## Validation

The updated server and demo working tree passed:

- PocketContext `make test` with the race detector across all packages and
  `make build` using Go 1.27.1 and CGO. New concurrency and HTTP regressions also
  passed three repetitions. The demo pins local server commit
  `2fb371c3466c7a5b65adbd6cf95309b46d27c4ee`; the rebuilt binary reports that exact
  revision with an unmodified tree.
- Demo unit/process suite: 231 tests, eight skips (seven macOS-only helper cases
  and the host nginx suite). Server and Litestream reset/replication gates were
  enabled using Litestream 0.5.17.
- Real-server `demo_backend`, `identity_rewrap`, `integration`, `archive_migration`,
  `realtime`, `limits`, `backup_integration`, `auth`, `oauth_integration`, `skill`,
  `cli_forward`, `deploy`, and `maintenance` checks, plus the protected contention
  regression.
- Copied standalone launcher `skill` and `cli_forward` checks against the updated
  server. Package source and launcher pin are unchanged.
- Offline OAuth, deployment workflow, entrypoint and object-storage settings
  checks; Go supervisor tests; Playwright demo browser suite.
- After pin/manifest adoption: four compatibility, 18 ONCE runtime and 18 standard
  runtime tests passed with the new server and real Litestream, without skips.
  The predecessor test exercises historical durable-schema admission and retained
  records with the candidate binary on both starts; it is not a historical-image
  deployment test. CI now runs this process gate and the protected contention test.
- DealContext integration, portable skill, deployment and intake checks against
  the updated server, preserving shared-workspace behavior. DealContext source
  and its server pin are unchanged.
- Final ARM64 container configuration, smoke, populated S3/Litestream recovery,
  standard Go-supervisor replacement/restart and seven real-nginx proxy tests.
  Recovery includes frozen restart, destruction of source storage, restored
  logical records, explicit thaw and independent protected-file authorization.
  The replacement gate preserves ciphertext and withdrawn contact preferences,
  checks all three databases and rejects an overlapping writer. Synthetic
  containers, networks and volumes were cleaned up.

The local container candidate was
`sha256:f9872c5db55af84e3ecec420d32f4d1da1ba022616937c2f78f8a189ec7840ca`.
A temporary allowlisted context replaced only the server Git-fetch stage with an
archive of exact commit `2fb371c3466c7a5b65adbd6cf95309b46d27c4ee`; module
verification and compilation ran inside the original pinned build image. The
tracked Dockerfile retains normal remote fetching. The application revision
marker was base `194f724` plus the working-tree changes, so this is local candidate
validation, not release attestation. AMD64/macOS release matrices, remote-fetch
reproducibility, real Google sign-in and cloud-provider checks remain unverified
by this run. Host nginx coverage omitted in the unit run was covered by the seven
container nginx tests.

At the end of initial local validation, the server commit and demo changes were
unpublished and undeployed. The authorized release follows server publication,
then the normal demo CI and deployment workflow; its outcome is recorded in a
separate release receipt.
