# SQL contention reproduction, 2026-10-11

The audit's cross-user SQL availability concern is reproducible on the original audit
server pin `976ddf7`. One enrolled synthetic visitor issuing four concurrent expensive reads
caused another visitor's ordinary directory read to fail with HTTP 408 after five
seconds. This demonstrates temporary cross-user denial, not persistent failure,
data exposure, or denial of all HTTP endpoints.

## Reproduce

From this repository after `uv sync --locked`:

```sh
uv run --locked python tests/demo_sql_contention.py --binary /absolute/path/to/pinned/pocketcontext --uncapped
```

`--uncapped` removes only the new per-requester setting from a temporary copy of
the configuration, so the original pinned server can still reproduce the finding.
The maintained configuration is never modified.

The diagnostic creates a loopback server with isolated temporary databases, a
synthetic Google OAuth provider and a local retention service. Two users enroll
through the real HTTP paths. It measures three baseline reads for the second
user, starts four concurrent unbounded recursive aggregate queries for the first
user, waits 250 ms and measures a second-user directory read. Server query
deadlines bound the expensive requests. It measures recovery after they finish.
No application documents, real accounts, provider credentials or live deployment
are used. The test has no remote URL option. All requests share a loopback IP and
remain below the configured request-rate limit.

## Observations

| Measurement | Run 1 | Run 2 |
| --- | --- | --- |
| Ordinary baseline median | 20 ms | 17 ms |
| Ordinary read under contention | HTTP 408, 5.006 s | HTTP 408, 5.005 s |
| Busy visitor request durations | 5.006–10.010 s | 5.016–10.021 s |
| Ordinary read after contention | HTTP 200, 11 ms | HTTP 200, 9 ms |

A later `--uncapped` rerun of the isolated-config harness returned HTTP 200
after 4.762 s for the other visitor (4 ms baseline), rather than HTTP 408.
Queued requests can time out before obtaining the freed slots, allowing the
ordinary read to finish just before its deadline. The precise status depends
on scheduling; both delay and denial were observed.

Both initial runs used `/tmp/vaultcontext-demo-tools/pocketcontext`. Its Go build metadata
identifies clean commit `976ddf71a4734530adefe4a56633658a0894b449`, Go 1.27.1,
Linux/arm64 and `CGO_ENABLED=1`. Binary SHA-256:
`de54e55e3303ea54f9ebd3c0b38000b4345618e64b1d988c11fe9d0a3cb60e08`.
The application checkout started at `194f72490fd27558fadacb20e58d93a981ebc76c`;
the audit remediation working tree was active, with the same two global snapshot
slots and five-second snapshot/query deadlines. These are local measurements,
not production capacity figures or a sustained/distributed attack simulation.

## Interpretation and mitigation

The server holds each global snapshot slot through query execution and cleanup.
Waiting for capacity consumes the snapshot deadline; query execution has a
separate deadline. This explains both the second user's timeout and expensive
requests taking approximately ten seconds when queued.

The implemented mitigation is a configurable per-authenticated-requester in-flight
limit, enforced before acquiring a global snapshot slot and held through cleanup.
With one request per requester and two global slots, one account cannot occupy
both slots or place additional expensive work in the shared queue. Excess work
return HTTP 429 promptly. Admission and release are atomic and account tracking
is removed when requests finish. Server regressions cover same-account rejection,
second-account progress, cleanup after timeouts/cancellation and snapshot privacy.

Increasing global concurrency alone consumes more resources and leaves the same
monopolization path. Shorter timeouts reduce individual disruption but do not
establish requester fairness. An IP-only limit also groups legitimate visitors
behind the same network and does not isolate accounts. Per-account admission
still cannot prevent multiple enrolled accounts from occupying all slots; broader
service capacity and abuse controls remain separate decisions.

The diagnostic reports whether contention occurred and asserts successful
baseline/recovery reads. Add `--expect-protected` with the updated server and
`snapshot.maxConcurrentPerRequester: 1` to require the other visitor's read to
succeed within one second and three excess busy-account requests to return
HTTP 429 within one second. Without that flag, the contention result is
characterization evidence, not a passing regression guarantee.

The remediation adds an optional admission limit to PocketContext, defaulting to
disabled for existing applications. SQL snapshots and private-search filtered
reads share account admission. The demo intentionally adopts current server
branch `private-scoped-search` at `dc1dc6b` plus this fix; the intervening private-search feature is
unconfigured in this application. The clean tested server commit is
`2fb371c3466c7a5b65adbd6cf95309b46d27c4ee`. Its `make test` race suite and
`make build` passed; the new requester and HTTP regressions also passed three
repetitions under the race detector. The rebuilt binary identifies this exact
commit with `vcs.modified=false`.


## Protected result

With the remediated server and demo cap of one, the same diagnostic with
`--expect-protected` passed: the ordinary visitor's read returned HTTP 200 in
10 ms during contention (5 ms baseline and 5 ms recovery). Three excess busy
requests returned HTTP 429 in 7, 8 and 14 ms; the one admitted expensive query
reached its normal deadline at 5.009 s. Thus the measured single-account denial
path is mitigated while preserving the query deadline and independent filtered
visibility. This does not establish capacity against multiple abusive accounts.

```sh
uv run --locked python tests/demo_sql_contention.py --binary /absolute/path/to/updated/pocketcontext --expect-protected
```
