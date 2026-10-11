# Application-owned standard supervisor

The ONCE image keeps application-specific lifecycle code in this repository.
`supervisor/cmd/demo-supervisor` builds a Go executable alongside the normal
PocketContext binary from `POCKETCONTEXT_VERSION`. It does not embed PocketContext
internals or call Docker, ONCE, PocketDeploy or GitHub.

With `VAULTCONTEXT_DEMO_ONCE=true`, tini starts the entrypoint, which execs Go.
Go starts the Python storage worker and owns signal forwarding, its process group,
the readiness listener and an 850-second total shutdown deadline. Configure the
host's stop timeout to at least 900 seconds. Worker subprocess output is suppressed;
failures report fixed diagnostics rather than credentials or child output.

The worker in `deploy/demo/once_runtime.py` retains database, contact, replication
and UTC-reset mechanics. It holds the exclusive volume lock for its lifetime;
the directly managed application and contact supervisors inherit that lock. Three databases and existing volume layout stay
unchanged: disposable `runtime/pb_data/data.db` and `auxiliary.db`, retained
`persistent/contacts.db`, and private durable `control/` state. The Go wrapper does
not replace Litestream or the tested Python storage helpers.

## Startup and upgrades

Startup closes ingress before examining private configuration and operation
markers. Existing `control/runtime.json` remains authoritative. A complete set of
environment bindings can establish configuration for a genuinely fresh volume,
or confirm an identical existing binding. Partial or conflicting bindings fail;
ordinary deployment cannot silently change storage or OAuth configuration.

`deploy/demo/compatibility.json` declares a server/migration fingerprint and
explicitly accepted predecessor fingerprints. Build-time verification compares
the declaration with `POCKETCONTEXT_VERSION` and migration filenames/content.
Changing either requires a reviewed compatibility declaration. The initial
transition accepts only explicitly listed legacy revisions without a fingerprint;
check the actual deployed revision before publishing a transition image.

An upgrade records a private, fsynced `control/upgrade-pending.json` before starting
writers. It includes source/target identities, schema identity, phase and a logical
baseline of all three databases. The worker starts the candidate while ingress
is closed, verifies replication, current generation and preserved records, then
commits durable state before opening traffic. A changed image revision alone
does not authorize arbitrary migration. Unsupported predecessors stay fenced.

The `prepared` and `validated` journal phases have explicit restart paths.
`starting` is intentionally ambiguous: migrations may have committed, so startup
stays fenced for operator recovery. There is no automatic rollback, journal
deletion to force retries, or restart of old code over a changed database.

If a compatible startup encounters an expired UTC generation, reset removes
disposable databases/ciphertext/replicas and retains contacts under their separate
policy. The reset's own durable marker fences interruption. Expired or interrupted
operations never receive another day of public access merely because deployment
crossed midnight.

Fresh initialization, missing-state recovery and existing-volume migration remain
distinct. An unconfigured volume exposes private control only; readiness fails.
Explicit initialization uses the private control `initialize` operation on an empty
installation with no prior durable state; it requires empty primary object storage
and no unfinished uploads, and does not purge remote storage. Application and
contact initialization refuse populated replicas. Missing/corrupt state, partial
databases, pending legacy releases or incomplete resets require recovery
review. Do not initialize or restore over an existing deployment to bypass them.

## Readiness and shutdown

The public nginx gate proxies `/up` to Go's loopback readiness endpoint. Success
requires the worker to report ready in today's UTC generation; the JSON response
contains `ready: true` and the baked application `revision`. Unconfigured, fenced,
stopping and stale-generation states are not ready. Other public routes remain
closed until the worker has completed verification. A process merely being alive
is insufficient for deployment success.

On termination, Go stops reporting ready and asks the worker to drain. The worker
closes public ingress and gracefully stops application and contact processes,
including their replication supervisors, before releasing the volume. A deadline
or failed drain produces a failed exit, which stop-first delivery must reject.

## Delivery transition

The private `pocketdeploy-v1` repository stages a `latest` application declaration
with stop-first delivery, 900-second stop/readiness budgets and independent DNS.
Its cutover runbook requires publishing the new PocketDeploy adoption feature,
pausing/draining/fencing the old dispatcher, verified same-volume adoption, then
standard convergence and new restricted GitHub access. Keep a single release
authority. The old prepare/validate/commit controls are unavailable in standard
runtime mode; historical helpers are retained for legacy test and recovery context.

The workflow's trigger selects the host-configured image at pull time, then checks
the actual running revision's published assets. Publication, adoption, cloud
verification and live migration are separate from these local source changes.

## Validation

Run Go tests from `supervisor/` and the README's complete application validation.
Additional synthetic runtime checks:

```sh
python3 deploy/demo/verify_compatibility.py deploy/demo/compatibility.json
python3 -m unittest discover -s tests -p test_demo_standard_runtime.py
VAULTCONTEXT_TEST_BINARY=/path/to/pinned/pocketcontext \
VAULTCONTEXT_TEST_LITESTREAM=/path/to/pinned/litestream \
  python3 -m unittest discover -s tests -p test_demo_standard_runtime.py
```

Container config, smoke, exclusive-volume startup, standard replacement and
populated recovery remain release gates. Mocked state tests alone do not establish
real storage or provider recovery.

## Health polling and request logs

The worker checks UTC expiry and child-process exits on its 200 ms loop. Once
startup readiness succeeds, HTTP probes of the application and contact service
run every 10 seconds using a monotonic clock. An unresponsive service whose
process remains alive can therefore take up to that interval plus request
timeouts to fence; startup still probes immediately.

Each worker generates a private, memory-only health token and passes it to its
application child and loopback status probe. The exact demo status route omits
successful request logs only for a matching token from a loopback transport peer.
Failed probes, public requests and invalid tokens retain normal logging. The
token is not an application credential and grants no record access.

This avoids filling `auxiliary.db` and its Litestream history with the worker's
own successful probes. Main and auxiliary replication remain at their configured
intervals; daily UTC reset remains responsible for deleting existing disposable
replica history. A routine release preserves today's sessions and does not
purge existing history.
