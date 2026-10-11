# Common validation and deployment contract

`image.yml` owns push, pull-request and manual validation. It calls `test.yml`
once per run; the test workflow has no independent push/PR trigger. Each app's
backend, authentication, client and frontend checks remain application-specific.
Both application tests and container configuration/smoke/populated recovery run
on native AMD64 and ARM64 Linux. VaultContext additionally tests its client and
memory-session behavior on macOS. Tests use isolated synthetic data only.

Publication from `pocketcontext/vaultcontext-demo` on `main` requires all
application and container checks and `VAULTCONTEXT_DEMO_PUBLISH=true`. Each
architecture is built on its native runner and published by digest; the manifest
uses exactly those two digests. All external actions are pinned to full commits.
Pull requests and other branches validate but cannot publish or deploy.

The `demo_manifest` job checks the current main revision and publishes
`ghcr.io/pocketcontext/vaultcontext-demo:latest` and `sha-<full commit>` tags.
It records the immutable multi-platform index digest in the job output, summary
and `demo-image-metadata` artifact. These publication artifacts identify that build; delivery reconciles the configured
`latest` tag and may select a later published build.
This repository has no production VaultContext publication or deployment path.

Demo CD is a separate `demo_deploy` job after `demo_manifest`. Enable it with
repository variable `VAULTCONTEXT_DEMO_DEPLOY_ENABLED=true`; set
`VAULTCONTEXT_DEMO_DEPLOY_PAUSED=true` to pause deployment while keeping validation
and publication available. The fixed GitHub environment is `pocketdeploy-v1`.
PocketDeploy installs `SERVER_IP`, the configured operator `SERVER_USER`,
`SSH_KNOWN_HOSTS` and `SSH_PRIVATE_KEY` for the application's restricted key.
Restrict that environment to `main`. Strict host-key checking remains mandatory;
the workflow never discovers or trusts a host key during deployment. Both
workflow-level and demo deployment concurrency prohibit automatic cancellation.

Immediately before SSH, the workflow verifies that its commit remains current
main. SSH sends no remote command and empty standard input. The per-key forced
command selects the host-configured application and reconciles its declared
`ghcr.io/pocketcontext/vaultcontext-demo:latest` image under PocketDeploy's shared
lock. It resolves the image before its stop-first replacement. The workflow
accepts exactly `{"ok":true}` as the reconciliation receipt; this acknowledges
successful reconciliation, not deployment of the triggering workflow's revision.
No registry credentials, scripts, archives or application secrets are transferred.
`SERVER_USER` names the existing operator account, rather than assuming a
separate `deploy` account. The CI key must exist only in its restricted form.

The application supervisor owns upgrade preparation, recovery and commit. Its
public `/up` returns success with `ready` and the baked `revision` only when the
current UTC generation is serving. ONCE owns the application, route and named
volume. Whole-volume automatic backups stay disabled because they would retain
disposable vault data alongside durable contacts.

The public check reads the actually running revision from `/up`, validates its
40-character commit identity, and fetches the public launcher and assets from
that immutable commit in the dedicated GitHub repository. It compares served
HTML, JavaScript, CSS, legal pages and SDK bytes against those sources, checks
today's UTC generation and exact installation contract, and requires retired
routes to return 404. A second `/up` read must match the first so an overlapping
release cannot pass a mixed check. Redirects, oversized responses, unavailable
source commits, malformed readiness and stale assets fail verification. The log
names the verified running revision and requires it to equal the triggering
workflow’s `GITHUB_SHA`. An internally consistent older or newer release fails
this deployment check. This checks release identity and publication consistency, not Google sign-in or
backup durability. No automatic rollback occurs after a failure; investigate the
durable lifecycle state before retrying.

## Controlled delivery cutover

Keep `VAULTCONTEXT_DEMO_DEPLOY_PAUSED=true` while preparing the transition. Drain
any dedicated deployment and disable its former environment/key authority before
allowing generic delivery. The old dispatcher and generic reconciler use separate
locks and must never remain concurrent release authorities.

First release and validate the supervisor image with ordinary stop-first lifecycle
support. Use PocketDeploy's explicit verified adoption operation to retain the
existing ONCE application and named volume; never edit ownership state manually,
initialize the existing volume, or create replacement OAuth/storage identities.
Prepare private bindings and recoverable deployment state through the deployment
repository's operator runbook. Install the generic restricted key and
`pocketdeploy-v1` GitHub environment while CD remains paused. Verify readiness,
contact preservation, recovery and a no-op convergence before unpausing. Revoke
or retire the old dedicated dispatcher only as documented by the cutover runbook;
keep it disabled throughout generic verification. A failure requires inspection
of durable state, not simultaneous retries through both controllers.

Both promotion and deployment refuse stale, malformed or failed main lookups.
These checks reject old reruns; they do not lock Git refs, and main can advance
after a check. Concurrency serializes running releases but does not guarantee
commit ordering or execution of every pending run. Do not cancel a deployment
in progress. Host-local locks are not distributed fencing, and Litestream
remains asynchronous.

Old `deploy/install.py`, app-local deployment wrappers and any old bootstrap
commands fail closed. They must not overwrite shared authorized keys or restart
retained source state. New deployment provisioning, storage creation and DNS
changes require a separate explicitly authorized operation. Container `init` is
only for a genuinely fresh installation, never an existing volume or replica.

After source changes, run the repository's README validation and the isolated
workflow checks:

```sh
python3 tests/deploy_workflow.py
```

Validate workflow YAML with actionlint. Actual native CI and image recovery gates
remain required before release; local validation does not replace either platform.

The native image checks also run `docker/once_smoke.py` in isolated CI containers:
control-only bootstrap returns `/up` 503, public routes remain 503, invalid
configuration is rejected, a second supervisor cannot take the same volume, and
shutdown completes cleanly. These Docker calls are image tests, not deployment
operations. Deployment operations use ONCE.

`tests/test_demo_once_runtime.py` exercises state transitions and failure paths
with real SQLite fixtures. With `VAULTCONTEXT_TEST_BINARY` and
`VAULTCONTEXT_TEST_LITESTREAM`, it also runs the pinned server, contact service and
three local replicas through a process adapter, restores each replica, verifies
ciphertext equality/decryption and preserves withdrawn marketing preferences
across a validated release. This process test does not claim to exercise the
shipped container paths; the separate native image bootstrap check covers those
paths. End-to-end ONCE adoption and live provider behavior remain separate checks.

## Mirrored build bases

The application and MinIO recovery-test Dockerfiles pull their pinned Go and
Debian bases from public `ghcr.io/pocketcontext/vaultcontext-demo:base-*` tags, always
with the original upstream index digest. These tags are base images, not releases
of the VaultContext application. Both indexes retain every upstream platform.

`.github/workflows/mirror-bases.yml` copies the fixed upstream indexes using
Skopeo `--all --preserve-digests`, then verifies the destination index digest and
AMD64/ARM64 platform entries. It uses Docker's official ECR Public distribution,
with Docker Hub as an alternate source on retry, and the repository's temporary
`GITHUB_TOKEN` for publication. No new long-lived registry credential is needed.
The workflow runs only on main, on changes to that workflow or manual
dispatch, and never updates application release tags.

When updating a base, first update and run the mirror workflow, verify anonymous
GHCR access and the exact upstream digest, then update both Dockerfiles. Container
smoke and populated recovery checks remain required before publication. Mirroring
does not upgrade or rebuild the upstream images. Build tooling and other external
package downloads have their own upstream availability requirements.

For the initial repository cutover, run the mirror workflow and verify anonymous
access before running image validation. Pause the old repository's demo CD before
enabling the new repository's environment. Preserve the existing hostname, OAuth
client, named volume and replica configuration; do not initialize the deployment.
Complete the controlled delivery cutover above before enabling demo CD.

## Standard demo container replacement gate

`uv run --locked python docker/standard_demo_smoke.py --image IMAGE` exercises the
shipped Go supervisor with three isolated TLS MinIO storage identities, a named
volume and synthetic records. It first rejects initialization with an unreferenced
primary object before creating any database. A temporary read-only test hook creates a synthetic
Google-linked identity and token; no fixture hook enters the release image.
Normal REST enrollment, encrypted uploads, filtered SQL and protected downloads
then populate and verify the application. The test withdraws retained contact
preferences, rejects a concurrent volume writer, requires a clean stop, replaces
the image with a different baked revision of the same schema, and verifies the
three databases, ciphertext decryption and preferences after replacement and a
same-revision restart. TLS uses a test CA trusted only by the isolated containers.
This is a Docker replacement gate, not an ONCE control-plane or real Google test.
Both native image CI runners execute it before publication.

Daily reset uses the R2 adapter only for exact Cloudflare account endpoints.
Other HTTPS S3 endpoints use the version-aware adapter, including deletion and
verification of retained versions, delete markers and unfinished uploads.
Unsupported listing APIs or incomplete deletion block reset rather than silently
retaining disposable data. Durable contact storage is excluded.
