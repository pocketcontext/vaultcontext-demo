# Demo reset and durable contact operations

This is an executable implementation with synthetic local tests, not evidence of a deployed or provider-verified service. No resources, timers, accounts or mail subscriptions are created by committing these files. The existing VaultContext production deployment must not use these reset scripts.

## Storage boundaries

Use one dedicated private root named `vaultcontext-demo`, for example `/var/lib/vaultcontext-demo`, owned by the lifecycle operator and mode 0700:

- `runtime/`: every disposable database (`data.db`, `auxiliary.db`), SQLite journal, local upload, temporary file, cache and local replica generation. Mount the container's complete `/storage` under this directory. Do not keep runtime snapshots elsewhere.
- `control/`: durable reset lock, pending fence, and last completed generation. Never mount this inside disposable `runtime/`.
- `persistent/`: separately protected contact/preferences database; never inside disposable `pb_data`. The contact service account owns this directory, mode 0700, and its SQLite file, mode 0600. The parent traversal permission must permit that service account (use an explicit ACL or run the lifecycle under the same dedicated identity).

Use the role-specific dedicated buckets `once-v2-vaultcontext-demo-files` for primary files and `once-v2-vaultcontext-demo-replica` for disposable database replicas. Existing dedicated names beginning `vaultcontext-demo-` remain accepted for isolated installations; arbitrary `once-v2-` names and swapped deployment roles are rejected. Credentials must be bucket-scoped and distinct from production and each other. The reset deletes the **whole two buckets**, not just a current generation prefix. This is intentional: old ciphertext, orphaned objects and old database generations cannot remain restorable after a daily reset. Do not place durable contact backups in either bucket. Use a third dedicated bucket, named `once-v2-vaultcontext-demo-contacts-replica` (or an existing `vaultcontext-demo-contacts-<suffix>`), with its own credentials for contact replicas. The daily reset must never receive those credentials or delete that bucket.

External VM snapshots, provider backups, monitoring exports, caches and prior manually exported copies are not discoverable by this script. Do not enable them for ephemeral demo data unless their deletion is integrated into the fenced lifecycle. The reset cannot erase downloaded copies on a visitor's device. Provider physical media erasure is not verified by an application API delete.

## Three SQLite replicas

| Database | Replica destination | Daily reset |
|---|---|---|
| `runtime/pb_data/data.db` | Disposable bucket, `LITESTREAM_PATH` | Delete database and every replica |
| `runtime/pb_data/auxiliary.db` | Same disposable bucket, `LITESTREAM_PATH/auxiliary` | Delete database and every replica |
| `persistent/contacts.db` | Separate contact bucket and `CONTACTS_LITESTREAM_PATH` | Preserve; apply contact retention |

The application entrypoint generates a two-database Litestream configuration only in demo mode. Production retains its existing configuration. Startup requires both demo databases, checks their integrity and stored UTC generation, and waits for both replicas before serving. Restores stage both databases before installation; interrupted installation leaves a durable fence. Restoring yesterday's generation cannot extend its deadline. Uploaded ciphertext remains in the primary object bucket and is verified independently; Litestream replicates SQLite, not those files.

The contact supervisor is `deploy/demo/contact_replica.py`, using `contact-litestream.yml`. Its `CONTACTS_LITESTREAM_*` variables cover `BUCKET`, `ENDPOINT`, `REGION`, `PATH`, `ACCESS_KEY_ID`, and `SECRET_ACCESS_KEY`. The default sync interval is `1s`; snapshot retention defaults to `672` hours and cannot exceed that limit. Contacts use their own Litestream process and private control socket. Neither contact credentials nor the database belong in the application container's disposable storage.

Replication is asynchronous and does not supply an atomic snapshot across databases. A planned migration must stop all writers, synchronize and verify all three replicas, and preserve an explicit handoff before the destination accepts traffic. It must also preserve primary-object access and the current generation. A disk failure can lose writes that have not reached a replica.

## Executable lifecycle

`deploy/demo/reset.py --config /absolute/private/reset.json` chooses today's UTC generation. `--generation YYYY-MM-DD` resumes an interrupted generation. `--dry-run` validates bindings and reports the intended generation without invoking hooks, credentials or deletion. It may create the private control directory.

The private, operator-owned JSON configuration has this shape (these are placeholders, not an installed deployment):

```json
{
  "deployment": "vaultcontext-demo",
  "origin": "https://vault-demo.pocketcontext.com",
  "root": "/var/lib/vaultcontext-demo",
  "storage": {
    "kind": "r2",
    "primary_bucket": "once-v2-vaultcontext-demo-files",
    "replica_bucket": "once-v2-vaultcontext-demo-replica",
    "durable_bucket": "once-v2-vaultcontext-demo-contacts-replica"
  },
  "hooks": {
    "fence": ["/opt/vaultcontext-demo-operator/fence"],
    "stop": ["/opt/vaultcontext-demo-operator/stop"],
    "assert_stopped": ["/opt/vaultcontext-demo-operator/assert-stopped"],
    "initialize": ["/opt/vaultcontext-demo-operator/initialize"],
    "start": ["/opt/vaultcontext-demo-operator/start"],
    "health": ["/opt/vaultcontext-demo-operator/health"],
    "unfence": ["/opt/vaultcontext-demo-operator/unfence"]
  }
}
```

Hooks are argument arrays, never shell strings. They are a deployment-specific integration boundary and must be implemented and verified before scheduling. They receive the generation in `VAULTCONTEXT_DEMO_GENERATION`, `VAULTCONTEXT_DEMO_MODE=true`, `VAULTCONTEXT_DEMO_RESET_ROOT`, `VAULTCONTEXT_DEMO_RESET_PHASE`, and the absolute `VAULTCONTEXT_DEMO_RESET_FENCE` path. Hook output is suppressed, including errors; hooks must not write credential dumps to files or other logs.

1. Acquire the private nonblocking lifecycle lock; atomically persist `control/reset-pending.json` before touching the service.
2. `fence` must block all ingress, including direct API/file traffic, and disable automatic restart/rollout controllers. `stop` must gracefully terminate server and replication workers. `assert_stopped` must positively verify no writer or replication process remains; a successful shell no-op is not acceptable in deployment.
3. Delete every remote object/version/delete marker and abort multipart uploads; verify both buckets empty. Delete the complete local runtime directory, including auxiliary database and replicas, and recreate it privately.
4. Persist phase `purged`; `initialize` must create a fresh database with the new generation and dedicated configuration using the image's explicit `init` mode, never restore the old replica. Persist phase `initialized`.
5. `start` must start the fresh service with the new generation while ingress stays fenced. `health` must verify generation, authentication state, empty visitor data and file namespaces, and normal private authorization. Persist phase `healthy`.
6. Record the generation, run `unfence`, then remove the pending marker. `unfence` must be atomic/idempotent and lift ingress only after verified fresh state.

Any failure leaves the durable marker in place and attempts to fence/stop again. A retry fences, stops and purges again; it never assumes a partially completed purge succeeded. Repeating a fully completed generation is a no-op. A pending older generation must be resumed explicitly before advancing. The backend also rejects stale UTC generations, so a missed scheduler invocation cannot extend yesterday's visitor access indefinitely. A failed adapter can still leave external infrastructure in an uncertain state: alert an operator and verify ingress is blocked; the local marker alone cannot enforce a remote load balancer.

The provided systemd timer templates schedule reset at **00:00 UTC** and contact retention at 00:05 UTC, with persistent catch-up. Install only after adapting paths, service identity, private credential files, actual fencing hooks and startup-fence mounts. A full session is not guaranteed: a visitor arriving at 23:59 UTC has approximately one minute until reset.

The S3 adapter uses the existing dedicated `VAULTCONTEXT_S3_*` and `LITESTREAM_*` credential variables and requires bucket names to equal the configuration allowlist. Set region and HTTPS endpoint explicitly. The S3 adapter enumerates version history and delete markers; unsupported APIs and object-lock failures stop the reset. The explicit R2 adapter accepts only Cloudflare account endpoints and skips unsupported object-versioning APIs; it still removes all keys, multipart uploads and every Litestream generation. See [Cloudflare's API compatibility table](https://developers.cloudflare.com/r2/api/s3/api/). Remote adapters require `boto3` from the deployment's locked environment. No provider operations have been run during local validation.

For isolated local testing, use `"storage":{"kind":"local"}` and a loopback HTTP origin. Put synthetic objects and replicas under `runtime/`; all are removed with the databases. Never point test configuration at real data.

## Persistent contact service

For standalone synthetic development, run `deploy/demo/retention.py --database /var/lib/vaultcontext-demo/persistent/contacts.db`. Deployed operation uses the contact replication supervisor, which starts this service only after successful recovery and initial synchronization. It binds only `127.0.0.1:8781`; the application backend calls it using `VAULTCONTEXT_DEMO_CONTACT_URL` and a shared `VAULTCONTEXT_DEMO_CONTACT_TOKEN` of at least 32 characters. The service token belongs in private unversioned configuration, never JavaScript. The service does not send email, create a provider mailing list, or contact a prospect. Enrollment records preferences; a newsletter sender is a separate integration.

For a containerized app, loopback means the same network namespace. Deploy the service alongside the app in that namespace or supply a private authenticated transport explicitly; do not expose the standard-library HTTP server to the public internet. The systemd unit is a host-service template, not a claim that a bridged Docker container can reach the host's loopback.

Authenticated endpoints:

- `GET /preferences?subject=...`: current purpose flags and revision; absent contact returns revision zero. The subject comes from verified Google provider identity, never browser input or daily PocketBase IDs.
- `POST /enroll` or `/preferences`: `subject`, verified `email`, `salesContact`, `newsletter`, `termsVersion`, `consentVersion`, and `expectedRevision`. Both purpose flags must be explicit booleans. Updates conflict with HTTP 409 if a newer choice or withdrawal exists. The backend must retrieve current preferences and let the person decide again, never blindly retry stale opt-ins.
- `POST /touch`: `subject`; updates last use without modifying consent or suppression.
- `POST /security`: exact `event` (`enrollment`, `auth_success`, or `auth_failure`), canonicalizable IP address `ip`, and integer epoch-seconds `timestamp` within five minutes. Same event/IP is deduplicated for 60 seconds. Only the backend may call it. Current integration records enrollment outcomes, not a complete attack or reverse-proxy log.
- `POST /maintenance`: `{}`; returns aggregate expired-contact and annual-review counts only.

An enrollment response returns an opaque `unsubscribeToken`; only its SHA-256 is stored. Unauthenticated `POST /unsubscribe` with `{"token":"..."}` withdraws both purposes and records suppression. GET requests never unsubscribe. Existing capabilities remain valid across preference updates so old unsubscribe links do not silently stop working. A public preference frontend must submit this capability over HTTPS without query-string/referrer/analytics logging; expose only the narrow proxy route, never the private service. No public unsubscribe proxy or mailing provider is provisioned by this operations module.

The SQLite store is a separate operational service, not direct access to any application's CRM database. It stores minimal contact information, versioned consent evidence, revisions, token hashes and purpose-specific suppressions. Request bodies, email addresses, subjects, tokens and full URLs are never logged. The separate security table stores the explicitly ingested event, IP and timestamp under the 30-day policy. Connections enable SQLite secure deletion, and Litestream uses WAL files in the same private directory. Secure deletion does not erase historical replica snapshots or guarantee physical media erasure.

## Retention contract

- Nonmarketing contact/profile records expire **30 days after last demo use**. Their consent events and unsubscribe capabilities are removed too. A first-time visitor who declines both optional choices does not acquire an indefinite suppression record.
- Opted-in marketing contacts are retained separately and become **due for review after 12 calendar months without explicit marketing engagement**. This is a review trigger, not automatic deletion or automatic renewal. Operators must justify continued use and remove unnecessary records. Maintenance reports the number due; it does not send marketing or silently move the review date. A new explicit affirmative purpose choice during enrollment updates that purpose decision and restarts the review clock; passive login/use via `/touch` does not. Ordinary demo activity is not itself marketing engagement.
- Explicit withdrawals retain only minimal purpose-specific email suppression outside the reset. `/touch`, another Google login, or resetting the demo never reverses suppression. A subsequent explicit, revision-checked opt-in may change it.
- Operational `security_events` are purged after **30 days**, except records with an explicit finite `incident_until` hold. General webserver/provider logs must be configured to the same policy separately. Do not use indefinite holds. The contact service does not collect request bodies or identity-bearing access logs.
- Durable contact snapshots have a maximum configured retention of 30 days. Historical snapshots may contain data already removed from the live database for up to an additional 30 days; a 30-day live-data rule is not a promise of erasure from every backup within 30 days of collection. Provider versions, snapshots and exports need matching policies. Confirm actual expiry behavior with the provider before launch.
- An ordinary recovery from an asynchronous contact replica conservatively disables restored marketing permissions and preserves suppression before serving. A verified, fully stopped host handoff can preserve current permissions. Retention is reapplied during recovery. There is no sender integration; any future sender must enforce current suppression and resolve historical deletions before sending. A restore must never silently reactivate an old mailing list.

The public promise is therefore **daily deletion of demo account and vault data**, with these separately disclosed contact/security exceptions. It must not claim every datum held by every system disappears at midnight.

## Validation

Run `python3 -m unittest discover -s tests -p test_demo_reset.py` from this repository. Tests use temporary synthetic databases and filesystem trees, a loopback HTTP service, failure hooks and mocked remote operations. They exercise fail-closed reset, interrupted reset, idempotence, stale preference conflicts, capability unsubscribe and retention boundaries. Before release also exercise real isolated S3/R2 pagination, partial failures, restart prevention, generation rollover, fresh initialization, ingress fencing and external backup/log policies. No deployment-readiness claim follows from unit tests alone.

## Concrete isolated Linux lifecycle

`deploy/demo/local_lifecycle.py` supplies actual hooks for a loopback-only synthetic deployment using the pinned PocketContext binary. It persists a launch intent before spawning; the child durably records its PID and Linux start ticks before executing the server. A pending handoff or an unknown listener fails closed. It independently probes exclusive loopback-port ownership before asserting that writers stopped, strips inherited S3/Litestream/AWS and production OAuth credentials, and stops gracefully before deleting, initializes fresh databases, validates empty visitor collections, and starts the new generation. Hook/server output is suppressed. This is not a public ingress controller: all listeners are loopback, and the local fence closes the process listener. Never use these hooks for public hosting or a process managed by an automatic restart controller.

Create the private root/config with:

```sh
python3 deploy/demo/local_lifecycle.py configure --root /absolute/temporary/vaultcontext-demo --binary /absolute/path/to/pinned/pocketcontext --port 8782
```

Start the contact service using that root's `persistent/contacts.db`, configure the private contact URL/token in the lifecycle environment, then run `reset.py --config ROOT/control/reset.json`. Stop the fixture with `local_lifecycle.py stop --root ROOT`. The local adapter initializes by briefly running the pinned server on loopback and stopping it; it does not run the production container's S3/replica entrypoint.

Run the real pinned-server lifecycle check in addition to unit tests:

```sh
VAULTCONTEXT_DEMO_TEST_BINARY=/absolute/path/to/pinned/pocketcontext python3 -m unittest discover -s tests -p test_demo_reset.py
```

This check runs the real server through initialization, stop, complete reset, reinitialization and health checks, verifies auxiliary database recreation and durable contact survival, and stops every fixture process.

## Operator review and privacy removal

Annual review is actionable through a **local operator-only CLI**, not a public endpoint or application capability. Run it as the authorized contact-store OS identity on the trusted host. Protect its input/output as personal operational data; never send queue output into shared logs, marketing exports, or chat transcripts. No email addresses are exported by the queue.

Use `retention.py --database /private/contacts.db --review-queue` with a JSON request on standard input: `{"after":"","limit":100}`. Results contain only stable subject, current revision and review date for due contacts. Pass `nextAfter` as the next request's `after` until it is null. Run decisions with `--review`, also through private stdin, using this shape:

```json
{"subject":"synthetic-provider-subject","expectedRevision":3,"decision":"retain","reason":"Current evaluation; continued need reviewed"}
```

`retain` requires an existing marketing permission, advances the revision and next review date by 12 calendar months, and records reason and timestamp in the restricted audit table. It cannot add a permission, reverse suppression, change last demo use, or send a message. Use a concise nonpersonal justification; do not include email addresses, file content, or incident details. A stale revision fails with status 409; read the current private state and make a new decision instead of overwriting a withdrawal.

`decision:"delete"` removes the contact, its consent evidence, review history and all unsubscribe capabilities. It preserves an existing complete suppression record unchanged, and creates or strengthens minimal purpose suppression when needed to prevent an old list silently reintroducing the address. The deletion reason is validated but is not retained as another personal record. A later explicit user opt-in is a new purpose decision, not an automatic import.

For an end-user privacy removal request, verify the requester's identity through the approved support process, resolve the stable Google subject privately, and perform the revision-checked `delete` decision. Explain the minimal suppression exception. Apply the same removal to separately managed exports/backups and any future sender system under their published policy. Removing the persistent contact does not operate on application vault records; the daily reset still removes those separately. No new marketing message is sent during this process.

## Docker deployment and planned host migration

The Docker adapter uses a dedicated network namespace, a loopback-only ingress gate, immutable server/nginx image digests and private environment files. It does not modify ONCE production containers or provision DNS, TLS or buckets. Public TLS routing must target only the gate: a host-network proxy can use its published loopback port, while the existing bridged ONCE proxy must use the validated namespace anchor’s private bridge address on port 8080. A direct application listener on port 80 would bypass fencing. The maintained scaffold supplies a dedicated boot/reset/routing controller; ordinary ONCE app deployment must not manage this demo. The adapter fixes the trusted client-IP header to `X-Forwarded-For`; the backend selects its rightmost address and the gate preserves the proxy header without appending the internal peer address. This assumes DNS-only public routing directly to the trusted TLS proxy, with client-supplied forwarded headers overwritten or followed by the actual peer address there. Keep the proxy’s forwarded-header preservation option disabled and verify a forged-header request against the deployed chain. Enabling Cloudflare proxying or adding another upstream proxy requires a new trust-chain review before relying on client IPs for rate limits or security records. Run the lifecycle as its dedicated root operator. `runtime.env` uses literal `KEY=value` entries, not shell `export` syntax; never print it.

Configure the dedicated root using the adapter's `configure` command and reviewed image digests. Supply the primary, disposable replica and contact replica credentials through the private environment configuration. The three bucket identities and credentials must be distinct. Initialize durable contacts once with `contacts-init`, then use the reset lifecycle to initialize the disposable daily generation. Starting with a missing contact database requires a verified restore; ordinary startup must not silently replace lost contact data with an empty database.

Planned migration uses these lifecycle actions with `--root /var/lib/vaultcontext-demo`:

1. On the source, run `migration-freeze`. This takes the same lifecycle lock as reset, persists a source migration fence, closes ingress, stops application writes, forces and verifies the final paired database replica, and fences/drains contact writes before verifying the contact replica. A failure leaves the source fenced. Suspend source rollout and reset controllers as well; do not remove the source fences to make a restart succeed.
2. Securely transfer the reviewed deployment configuration and completed handoff manifests to the new host. Place the runtime manifest at `control/incoming-runtime-handoff.json` and the contact manifest at `persistent/incoming-handoff.json`, using private operator-owned files. Reuse the dedicated primary objects and replica destinations; do not run a fresh-init or daily purge against them while transferring. Do not copy credentials into a transcript or Git.
3. On the fresh target, run `migration-adopt`. Restore and compare the paired application databases and contacts with the verified handoff. Preserve the same UTC generation, application configuration and contact token. The target must accept populated state; reset's empty-database health condition does not apply to migration.
4. Verify the populated target through its private network and then run `migration-unfence` before switching the authorized TLS route. Confirm sign-in, authorization and protected ciphertext retrieval. Keep the source stopped and fenced, including its scheduler and deployment controller. After target verification, securely remove the old host's disposable local runtime and transient handoff copies before their midnight deadline. Do not run the old host's reset command against shared remote buckets: only the designated active host owns remote deletion.

Crossing 00:00 UTC does not grant another day to the old demo. An expired generation must remain unavailable and be purged through a coordinated reset on the designated active host; its contact store survives. Complete the public route change only after resolving which host owns the reset. Both hosts must never write the same replica paths concurrently.

The manifests attest verified final database state; they are not a distributed fencing service. Operator control of both hosts, load balancing, automatic restarts, timers and replicas remains necessary. Provider-backed migration, ingress isolation, image startup and populated recovery must pass before calling this deployment ready.

## Operator details for the test

The current public contact for privacy requests and support is `info@pocketcontext.com` (updated 10 October 2026). It is a Google Group intended for the operators. External delivery was observed, but test messages reached the receiving mailbox’s Spam folder; operators must monitor Spam while that classification is investigated. This contact update preserves the existing acceptance version.

### Original operator details

The user supplied Alberto Miorin (an individual, not an incorporated company), alberto.miorin@pocketcontext.com, and the correspondence address c/o Engelnest Coworking Space and Event Venue, Wilhelm-Kabus-Straße 24, 10829 Berlin, Germany. The user authorized proceeding with the test after stating that permission to publish/use the venue address has not been confirmed. This is an unresolved address verification issue, not an attestation of a registered office or legal compliance.

Contact replica retention checks use pinned Litestream 0.5.17: native snapshot
retention is capped at 672 hours (28 days). Before serving, and daily while
running, the supervisor drains its HTTP writer, stops replication, forces an
idle-safe snapshot, enforces native retention, restores and compares the logical
database digest, and checks listed LTX timestamps. A missing fresh snapshot or
listed file older than 29 days stops the contact service. The private
`replica-retention-check.json` records only check time and maximum listed age.
This is a monitored release safeguard, not proof of provider physical deletion:
unknown objects, provider copies, long outages, and conservative native retention
floors require operator review. No blanket provider TTL may destroy the last
recoverable baseline. Daily refresh briefly makes contact endpoints unavailable;
clients can retry. Failure needs recovery review before restarting.

## Browser authentication

The live page uses vendored PocketBase JS SDK 0.28.1 and default LocalAuthStore
(`pocketbase_auth`). Reload restores authentication only after server auth-refresh
and preference validation. Terms acceptance and enrollment UI are never restored.
Refresh uses an isolated memory store and an attempt generation guard so a late
response cannot resurrect a signed-out or replaced session. Cross-tab changes
clear the visible identity; refresh the page to validate an account selected in
another tab. Blocked browser storage falls back to memory-only authentication.
The existing explicit OAuth popup/SSE wrapper preserves COOP and cancellation
handling. Website sign-out clears browser auth, not CLI auth or server tokens.
The daily reset guard clears browser auth when observed; closed browsers can
retain stale tokens until their next visit. Vault secrets and withdrawal
capabilities are not persisted in browser storage.

## Coding-agent installation

The public skill at `https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext` is the primary agent setup path.
The website copies an instruction prompt containing `npx skills add` with the
explicit branch/skill URL; it does not run installation itself. Node.js/npm and uv
are prerequisites. The agent loads the installed skill, uses its pinned launcher,
and guides the user through browser enrollment, Google login and private terminal
initialization/unlock. Installation never counts as enrollment or unlock.
The page is served directly at `/`; legal pages use `/terms/` and `/privacy/`.
Old `/demo` and hosted download paths return 404 without compatibility redirects.
Status installation metadata identifies the public skill and launcher client pin.
Missing/invalid metadata keeps setup unavailable. This does not probe upstream
GitHub availability. Existing wheel-backed launchers require replacement.
The API paths, OAuth callback and three-database recovery contract are unchanged.
