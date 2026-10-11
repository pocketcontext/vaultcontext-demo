# Upstream review — 11 October 2026

Reviewed the locally available core `upstream/main` through
`86906ae6cc0ea3231b8f452adcb98db77cfd1aa4` against demo base `194f724`.
This is a source compatibility review, not evidence of a deployment or a fresh
remote-ref lookup. The demo keeps its existing client pin. Its server adoption is recorded below.

| Core commit | Demo disposition |
| --- | --- |
| `64ac04800f8e12681fd45673abae04ec21b7759b` | Adopt the qualified, action-specific `audit_log` SQL filter as a selective port. Actor-private empty-vault events are limited to `identity_rewrap`; membership-based vault visibility is unchanged. Signed identity replacement, identity initialization checks and fingerprint-file protections already originated in the demo. The signature verifier and vendored verification library match this core commit. |
| `07521c0` | Exclude the core launcher pin and core release evidence. The demo launcher uses the separate public demo repository; its `1d88e0f4865405a1140a73cf9fbb5675a566cd13` package source still matches this checkout's `src/`, `pyproject.toml` and `uv.lock`. |
| `5058fb1d0a00859503c80901fd136c5acffd2d63` | Exclude release-header additions to core recovery downloads because the demo does not enable the core release gate. Keep the demo's independent populated recovery checks. |
| `86906ae6cc0ea3231b8f452adcb98db77cfd1aa4` | Exclude core validation/deployment receipts: they do not establish demo compatibility or deployment state. |

The matching-release mechanism in `64ac048` is intentionally excluded from this
change. It is a compatibility policy, not authentication. Its API-wide header
requirement would reject the demo's anonymous status, enrollment and withdrawal
flows and its browser client without coordinated adaptations. Core installation
URLs, release identifiers, password-login behavior and production deployment
configuration must not replace the demo's Google-only client and disposable
lifecycle. The existing advertised client revision remains an installation
contract, not a server-enforced release gate. Any future gate needs a separate
demo browser/CLI/session/recovery compatibility change and validation.

Core `64ac048` adopted shared fixes originating in demo `eadfcf6`, `1164339` and
`8b8b177`. Demo enrollment, contact retention, daily deletion and Google-only CLI
policy remain separate. This change does not modify or validate the core
application. The narrower audit policy is covered by `tests/identity_rewrap.py`,
including exclusion of unrelated account-level events and another actor's event;
the demo integration suite covers membership-based visibility.

## Server adoption

The demo advances from `976ddf71a4734530adefe4a56633658a0894b449` to
`2fb371c3466c7a5b65adbd6cf95309b46d27c4ee` and explicitly enables
`snapshot.maxConcurrentPerRequester: 1` alongside two global slots. The new
server commit adds optional authenticated-account admission before the shared
queue; zero/omitted remains backward compatible. It also includes existing
`dc1dc6b` private-search support, which remains inactive because the demo has no
search configuration. No PocketBase version or application migrations change.

The compatibility declaration adopts the new server/migration fingerprint and
retains the prior `761af213fb927c7ef87bb0c09941a2b0fed7e7b567f3c31d109fcf75e4f8f370`
predecessor. This allows the reviewed same-database upgrade without authorizing
unrelated schema transitions. Server race tests, demo application/backup/auth
checks and DealContext shared-workspace checks validate this adoption. Other
applications' pins are unchanged.

The server commit must be available remotely before the demo's normal CI/image
build can use this pin. It is published on `private-scoped-search`; this adoption
does not merge that branch into server `main`. Application release validation
and deployment evidence are recorded in the [release receipt](release-2026-10-11.md),
separately from the initial local review.
