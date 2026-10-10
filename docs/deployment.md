# Container deployment and recovery

This repository owns the disposable public demo at `vault-demo.pocketcontext.com`.
Use [demo operations](demo-operations.md) for reset, contact retention and recovery
boundaries, and the [CI and deployment contract](ci-and-deployment.md) for the
ONCE supervisor and exact-digest release protocol. The core container behavior
below remains useful for isolated tests; it is not a standalone demo launch
recipe. [Inherited release evidence](../DEPLOYMENT.md) describes historical core
VaultContext releases, not the dedicated demo.

The container runs one PocketContext/SQLite writer and Litestream publisher, serves
port 80 and `/up`, and stores SQLite under `/storage/pb_data`. Primary files are
private S3 objects. `docker/entrypoint.py` is the sole startup program; tini and
Litestream own normal signal forwarding and shutdown. No archive scheduler runs.
The server remains pinned by `POCKETCONTEXT_VERSION`. The image also contains
the demo supervisor, contact service and public onboarding assets.

## Configuration

Require all of `VAULTCONTEXT_S3_BUCKET`, `VAULTCONTEXT_S3_ENDPOINT`,
`VAULTCONTEXT_S3_REGION`, `VAULTCONTEXT_S3_ACCESS_KEY_ID` and
`VAULTCONTEXT_S3_SECRET_ACCESS_KEY`. `VAULTCONTEXT_S3_FORCE_PATH_STYLE` defaults to
`true` and accepts only `true` or `false`. Use private primary-file storage with
credentials scoped to that bucket. Configure separate `LITESTREAM_BUCKET`,
`LITESTREAM_PATH`, `LITESTREAM_ACCESS_KEY_ID` and `LITESTREAM_SECRET_ACCESS_KEY`.
The file bucket and access key must differ from the replica bucket and access key.
Set `LITESTREAM_REGION=auto` and the verified S3 endpoint for R2. The default sync
interval is 10 seconds. `LITESTREAM_DISABLED` is unsupported.

Keep paired `VAULTCONTEXT_GOOGLE_CLIENT_ID`/`VAULTCONTEXT_GOOGLE_CLIENT_SECRET`,
the demo Google audience policy, optional paired operator email/password,
`BASE_URL`, trusted proxy and SMTP configuration unchanged for an existing deployment.
Secrets remain in the deployment scaffold's ignored private configuration. The
server child retains primary-storage credentials but drops replica/AWS credentials
and the bootstrap password. Errors suppress subprocess and SDK details.

Demo storage and contact replicas require three independent scoped identities;
see [storage boundaries](demo-operations.md#storage-boundaries). Core VaultContext
bucket names and historical production prefixes are not demo configuration.

## Start, initialize and recover

Ordinary startup preserves an existing database. When absent, it strictly restores
Litestream into a temporary sibling directory, checks SQLite and every referenced
remote file, fsyncs the database, then installs it and fsyncs the directory. Missing
replicas and partial local state fail closed. A failed verification installs no
database. Ciphertext is streamed and compared with stored SHA-256; avatars and any
other file fields are streamed for readability/completeness, without a hash guarantee.

Use the maintained ONCE demo supervisor for public hosting. Its control-only
bootstrap, configuration and initialization are described in the
[release contract](ci-and-deployment.md). Repository separation preserves the
existing volume and replicas; never run `init` as part of that cutover.
The generic entrypoint's initialization rules below apply to isolated container
validation and genuinely fresh installations only.

`init` requires an empty directory and no existing replica, applies migrations and
optional operator provisioning, verifies objects, and exits. Normal startup then
establishes replication before HTTP. Never run `init` for upgrades, production
restarts or recovery. Interrupted initialization leaves a durable
`initialization.pending`; preserve the failed volume rather than deleting the marker.

Frozen startup requires existing main and auxiliary databases and the private
`maintenance.json` marker. It skips restore and provisioning, checks stored S3
settings against configuration and verifies remote files. Pending migrations remain
rejected by the pinned server. Litestream does not recover `maintenance.json` or
`auxiliary.db`: carry consistent snapshots separately for a frozen host migration.
A writable recovery can recreate auxiliary state, not its prior history.

## Single-writer updates and rollback

Use the dedicated demo dispatcher in `once-pocketcontext-v2`. Preserve the volume,
OAuth identity, exact storage configuration and contact database. The ONCE
supervisor holds the writer lock and fences public traffic while validating a
candidate release; keep ONCE automatic updates and whole-volume backups disabled.
Never run a recovery copy against the active replica.

The main-only image workflow publishes `ghcr.io/pocketcontext/vaultcontext-demo`
after all native validation gates. `VAULTCONTEXT_DEMO_PUBLISH=true` enables
publication. `VAULTCONTEXT_DEMO_DEPLOY_ENABLED=true` enables deployment through
the fixed `vaultcontext-demo` GitHub environment, restricted to `main`.
`VAULTCONTEXT_DEMO_DEPLOY_PAUSED=true` pauses newly evaluated deployment jobs
without pausing validation or publication. Let an active deployment finish before
treating the host as fenced.

The environment holds the dedicated `SSH_PRIVATE_KEY` and `SERVER_IP`,
`SERVER_USER=deploy`, `SSH_KNOWN_HOSTS`. The job checks the current main revision,
sends an exact image digest/revision request through the restricted SSH key and
validates the receipt plus public assets and installation contract. See the
[complete protocol](ci-and-deployment.md); `/up` alone is not a release attestation.

The app-local `deploy/install.py` and `deploy/deploy-vaultcontext.py` remain retired
fail-closed stubs. Manage host helpers through the private deployment scaffold.
There is no automatic rollback. Before any manual rollback, verify the previous
image's schema, supervisor and image-namespace compatibility and the pending
release state. Preserve current data; restoring an older database is a separate
recovery operation. Historical core VaultContext images and pre-S3 archives are
not demo rollback targets.

Litestream replication is asynchronous: clean exit alone does not prove final
remote durability. Verify synchronization and an independent restore. File retention
is independent of replica retention; a database cannot restore deleted objects.
Full ciphertext verification can increase readiness time. No recovery key exists;
the user's unlock passphrase is still required to decrypt files.

## Validation and release gates

```sh
uv sync --locked
python3 tests/entrypoint.py
python3 tests/object_storage_settings.py
uv run --locked python -m unittest discover -s tests -p 'test_*.py'
uv run --locked python tests/deploy_workflow.py
# Run all pinned-server checks listed in README.md.
docker build -t vaultcontext:check .
uv run --locked python docker/smoke.py config --image vaultcontext:check
uv run --locked python docker/smoke.py smoke --image vaultcontext:check
uv run --locked python docker/smoke.py restore --image vaultcontext:check
```

The smoke and populated recovery checks use synthetic MinIO with separate scoped
file/replica identities. `restore` invokes `docker/object_storage_smoke.py`, comparing
all main database rows before destroying the stopped source volume, retaining a
frozen snapshot, and exercising actual empty-volume recovery and late writes.
Unit tests exercise failed staged restores, initialization interruption, signals,
credential stripping, all-file inventory and corruption. Legacy archive helpers
remain test-only under `tests/legacy_backup.py` and are excluded from the image.
To reuse a trusted local MinIO fixture, `VAULTCONTEXT_TEST_MINIO_IMAGE` selects its
full `sha256:` image ID; otherwise tests build the pinned fixture.

Image CI gates publication on application and container checks; publication still
requires `VAULTCONTEXT_DEMO_PUBLISH=true`. The guarded demo deployment job follows
successful publication. Record dedicated release evidence separately from the
inherited core history. Historical results do not prove the new repository
cutover or a live deployment.
