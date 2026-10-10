# Historical core VaultContext deployment evidence

The records below were inherited from `pocketcontext/vaultcontext` with its Git
history. They describe past releases of core VaultContext at
`vault.pocketcontext.com`, not releases or current runtime state of this dedicated
demo repository. Historical image digests, repository links and commands are
preserved as evidence; do not use them as the demo deployment runbook.

For the dedicated demo at `vault-demo.pocketcontext.com`, use
[demo operations](docs/demo-operations.md), the
[CI and deployment contract](docs/ci-and-deployment.md) and
[repository separation](docs/repository-separation.md). The repository cutover is
in progress; this file does not attest its completion or a new live release.

## Full-name CLI — 4 October 2026

The public command is now `vaultcontext`, with no `vc` alias or compatibility
wrapper. The standalone uv launcher pins package
`0298c2c33abb3b8037d5624c237ab63c9b0437fb`; Python requirements, encrypted storage,
authentication, Keychain enrollment and memory-session behavior are unchanged.
No ObserveContext dependency or tracing was added. The public onboarding guide
uses the full-name command and documents locking old sessions before upgrading.

Local validation passed 90 unit tests (7 macOS-only skips), all twelve README
validation scripts and both copied remote-launcher checks against pinned server
`a92b0de5e1b66b6d3b6135b90092d2d6da5f7cc8`. Checks cover encryption, privacy,
Google OAuth, file limits, complete backups, independent launcher/session lifecycle,
revocation and byte-exact file output. Offline onboarding analytics and
320/390/1440-pixel keyboard, installation-switching and clipboard checks passed.

The installed workspace skill and `~/.local/bin/vaultcontext` were refreshed.
The verified old VaultContext `~/.local/bin/vc` launcher was removed after locking
the configured session. The new client passed ordinary live identity/schema checks.
The workspace skill lock update is committed as `6d3417c`.

Production runs source `9a7e0c28bda818e63cc44f83be3ee030bea2b83a`, pinned to
`ghcr.io/pocketcontext/vaultcontext@sha256:4b41228781e4c7464c1ba99c372c12834306bc77024645f41fa9a19e55ce495a`.
[Linux/macOS application CI](https://github.com/pocketcontext/vaultcontext/actions/runs/37200857449)
and [container recovery and multi-platform publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37200857582)
passed. A fresh complete database/ciphertext backup was uploaded, downloaded and
verified before deployment. The hash-verified installed wrapper ran with its
image fixed to this manifest, retaining its app lock, graceful stop and single
writer. Its installed file was unchanged.

The exact production source, image, server pin, one CPU, 512 MiB, disabled
automatic updates and one writer were verified. All fourteen sibling containers
retained their IDs, images, running states and resources. Public HTML matched
source after the existing Cloudflare Rocket Loader rewrite; other public assets,
security headers, health and anonymous-access rejection passed. Live browser
checks at 320, 390 and 1440 pixels passed keyboard navigation, installation
switching and copied full-name commands, without JavaScript errors or analytics
requests. Scaffold build
and create dry-run passed. No cloud resources, vault records or encrypted files
were changed by this release. Real Google sign-in and Keychain user-presence
flows remain user-device checks.

## Production-only website analytics — 4 October 2026

Production runs source `928f10c86c124c280c02ec2b86efdf794b4153f0`, pinned to
`ghcr.io/pocketcontext/vaultcontext@sha256:3b93448c975fd175874141f5ea5539bf714c9ecfaa07fc9bfed148b9c19d2005`.
Optional analytics activate only on `https://vault.pocketcontext.com/` after a
visitor accepts. Rybbit receives explicit fixed events without its replay script;
GA4 runs in an empty same-origin frame. Fixed metadata and allowlisted targets
exclude guide text, local paths and URL parameters. Both dashboards can filter
Hostname equals `vault.pocketcontext.com`. CLI and server pins are unchanged.

[Application CI](https://github.com/pocketcontext/vaultcontext/actions/runs/37197135639)
and [container asset, smoke, populated restore and publication gates](https://github.com/pocketcontext/vaultcontext/actions/runs/37197135835)
passed. Local README checks, offline privacy/browser tests, actual Google tag
verification with intercepted collection, desktop/mobile visual and keyboard checks
passed. Content-hashed JS/CSS URLs avoid stale CDN assets; container smoke checks
all public files including the analytics frame. Shared dashboard settings were
not changed. GA's automatic empty-frame engagement/scroll events are documented;
filter to explicit onboarding events to measure guide use.

A verified complete backup preceded the locked graceful update. The reviewed
wrapper was run with its image fixed to this manifest; its installed file remains
unchanged. Production revision, digest, server pin, one writer, one CPU, 512 MiB
and disabled automatic updates were verified. All fourteen sibling containers
retained their IDs, images, state and resources. Exact versioned public assets,
security headers and live desktop/mobile, keyboard, clipboard and no-JavaScript
checks passed. Live opt-in produced Rybbit HTTP 200 and GA4 HTTP 204 responses;
default-off and withdrawal behavior passed with no browser errors. Dashboard
report processing was not independently inspected. Scaffold build and create
dry-run passed; no cloud resources or application records were changed.

## Comparison prompt readability — 4 October 2026

Production runs source `1b318a6f4f2c5c6fdb7c2fe60306550d869e7ca5`, pinned to
`ghcr.io/pocketcontext/vaultcontext@sha256:ada04c443f653c340877c2b2238738c4157f3aab72a07806270136b3a6c0ded3`.
The comparison prompt uses readable paragraphs; copying preserves line breaks.
[Application CI](https://github.com/pocketcontext/vaultcontext/actions/runs/37194769730)
and [container recovery/publication CI](https://github.com/pocketcontext/vaultcontext/actions/runs/37194769810)
passed. A verified complete backup preceded the locked graceful deployment.
Live exact assets and desktop/mobile, keyboard, clipboard and no-JavaScript checks
passed without browser errors. Production revision and image match the pin;
resources, server pin, deployment wrapper and all fourteen sibling containers
remain unchanged. Scaffold build and create dry-run passed.

## Cross-computer comparison and restore guide — 4 October 2026

Production at https://vault.pocketcontext.com runs
`c41c2865910e13619866d732ffbfad37927b7b48`, pinned to manifest
`sha256:550886572fb72a3dfe8549defbce54603b6148d071039bbb4f351b3071c90aa9`.
The public guide at `/#another-computer` demonstrates synthetic prefix mapping,
comparison reports, selected version-pinned restores and verification. Agent
prompts and CLI equivalents preserve private unlock, explicit overwrite and
user-only diff. The workflow reference and installed skill were refreshed.
The launcher remains pinned to tested client
`6c383279ea2b8d5f721196b5df46e19a405ecffc`; no client implementation changed.

[Linux/macOS application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37193527231)
and [container configuration, smoke, populated restore and multi-platform publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37193527386)
passed. All local README checks passed, plus an isolated synthetic cross-computer
scenario and desktop/390/320-pixel browser checks; see [validation](docs/validation.md#compare-and-restore-across-computers-2026-10-04).

A complete verified database/originals backup succeeded immediately before the
update. Existing operator SSH verified the installed deployment wrapper against
reviewed source. The wrapper ran in memory with only its fixed image changed to
the tested digest and its guard extended to accept the previous `latest` reference.
Its persistent file, lock, graceful-stop, sole-writer and recovery behavior were
unchanged. No full scaffold convergence or cloud provisioning occurred.

Production matched the exact revision, digest and server pin. One running writer,
one CPU, 512 MiB and disabled automatic updates were verified. All fourteen sibling
container IDs, images, running states and settings were unchanged. Public health,
exact CSS/JS/image bytes and HTML after Cloudflare's existing script transformation,
security headers, anonymous schema/query rejection and source-path denial passed.
Live desktop/390/320-pixel layout, keyboard disclosures, installation switching,
copy success/fallback and no-JavaScript access passed without browser errors.

The deployment scaffold pins the same manifest; its build and create dry-run passed
using the installed Java runtime. No vault records, schema, server pin or encrypted
file contents were changed or inspected. Real Google and Keychain user-presence
flows were not exercised for this static-guide release.

## Encrypted comparison checksum release — 4 October 2026

Production at https://vault.pocketcontext.com runs
`c4ae54849afe99048a6c5fb37b5386affa461520`, manifest
`sha256:f22e72b569705259c6b96051f59e216f2870a819b1f171cca159bd72a30c5d1a`.
The portable launcher pins client `1e8432ba7a6ba1a069667591cb667c5ff48abf1d`.
New saves put plaintext SHA-256 inside encrypted, signed metadata; `vc compare`
returns only equality and document/version/method identifiers. New versions need
no file-chunk download for comparison; older versions use authenticated in-memory
comparison without temporary plaintext files or history changes. Restore, cat and
export verify the plaintext checksum when present. The onboarding page documents
comparison and its limits; the main marketing website required no change.

[Linux/macOS application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37188779894)
and [container configuration, smoke, populated restore and multi-platform publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37188779992)
passed. The complete local README suite, both copied-launcher release checks and
final ARM64 image configuration/smoke/populated-restore checks passed; see
[local validation](docs/validation.md#encrypted-comparison-checksums--2026-10-04).
The package staging commit skipped CI; the final pinned release ran every gate.

A pre-deployment complete database/ciphertext backup uploaded successfully. The
installed wrapper hash matched reviewed source; existing operator SSH invoked its
locked graceful-stop update. Production revision, digest, unchanged server pin,
one running writer, one CPU, 512 MiB and disabled automatic updates were verified.
All fourteen sibling container IDs, images, states and settings were unchanged;
all public application/website health endpoints returned 200.

Public HTML matched source after the existing Cloudflare Rocket Loader rewrite;
CSS/JS matched exactly. Security headers, health, anonymous schema rejection and
private source-path denial passed. Live desktop, 390/320-pixel mobile, keyboard,
installation switching, compare-command clipboard success/fallback and no-JavaScript
checks passed with no browser errors.

Installed skills, the workspace lockfile and the local PATH launcher were refreshed;
authenticated `vc check` returned compatible. Existing unlocked sessions were not
stopped: run `vc lock` then `vc unlock` privately to load the new comparison code.
No server/schema/cloud-resource changes or vault document rewrites occurred.
Real Google and macOS Keychain user-presence interaction remain separate manual
checks; this release did not perform an independent security audit.

## macOS and Keychain onboarding release — 4 October 2026

This earlier release ran
`ffc5a9abef8b2209f4c0da487a6a6cf3a27f58a4`, manifest
`sha256:e346b5095a0270150b5de18f50e48a93bbdd8af5ef5093e65f394079b0ba2b85`.
The page now documents Linux/macOS support, canonical temporary paths and
optional Keychain enrollment. Keychain use still requires a separately signed,
provisioned Apple helper; real device authentication remains unverified.

The portable launcher pins client
`58e94a3381f4fc8c25a5ed236142f8490ea5867f`. Both copied-launcher tests passed,
and the installed workspace skill, workspace lock hash and local PATH launcher
were refreshed and verified. Existing unlocked sessions were preserved.

[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37156427139)
and [container checks, populated restore and multi-platform publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37156427226)
passed. The installed deployment wrapper matched reviewed source. Operator SSH
invoked its locked graceful-stop update after verifying the published revision.
Exact revision, digest, server pin and public asset hashes passed, with one running
writer, one CPU, 512 MiB and automatic updates disabled. All fourteen sibling
container IDs, images, states and settings were unchanged.

Live health/security headers, anonymous schema rejection, private-source denial,
updated onboarding text and exact CSS/JS passed. Local desktop/390/320-pixel,
keyboard, installation-switch and clipboard checks passed; live mobile layout,
keyboard disclosure and Keychain command copying were also verified. No schema,
application records or cloud resources changed.

## Open Graph social card release

This earlier release at https://vault.pocketcontext.com ran
`0c07040e6c8c0101fc84df922a75e0c4f4af1c37`, manifest
`sha256:d1db50cfa540f99a920020ee0f47119c9354d9c2359bf77e7f627d5dabc6a6f2`.
The public `/og-card.png` is included in the image. Open Graph and Twitter
large-image metadata include the image URL, title, description and alt text;
Open Graph dimensions match the 1730 × 909 PNG, and the canonical URL is the
production origin. The launcher and server pins are unchanged.

[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37146303655)
and [container checks, populated restore and publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37146303948)
passed. The reviewed installed wrapper was invoked through existing operator
SSH access for the locked graceful-stop update. Exact revision and digest,
one writer, one CPU, 512 MiB and disabled automatic updates were verified;
all sibling container IDs and running states were unchanged.

Live ordinary, Twitterbot and Facebook crawler user-agent requests returned
matching metadata, canonical URL and exact image bytes with `image/png` and
correct dimensions. These checks verify fetchability, not a platform's cached
rendering. Public health/security headers, anonymous schema rejection, source
isolation, desktop/mobile/320-pixel layouts, keyboard navigation and clipboard
success/fallback passed. Cloudflare's existing Rocket Loader HTML transformation
remains present. No application data, schema or cloud resources changed.

## Bare-command help release

This earlier release ran `4f1881a14b45c350c23ad42ce0efc28fae75f1ce`, manifest
`sha256:ec0ca2967565eb0b81b0b8d060812bb354f5e16f6ba20684e157eec3ad66c9e9`.
The portable launcher pins `cd7020f72c7240845b2716405eb9e10d15c238f9`.
Bare `vc` now displays full help and exits 0 without configuration or sign-in;
invalid and incomplete commands still fail. The local PATH launcher was updated
and verified. Existing unlocked sessions were not stopped.

[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37141566277)
and [container checks, populated restore and publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37141566457)
passed. All README application checks and both copied-launcher release checks
also passed locally; see [local validation](docs/validation.md#bare-command-help-release--2026-10-03).

The installed deployment wrapper hash matched the reviewed source. The dedicated
SSH key was rejected; existing operator access invoked the same locked,
graceful-stop wrapper successfully. No deployment keys were changed. Verified
the exact production revision and digest, one running writer, one CPU, 512 MiB,
and automatic updates disabled. Sibling container IDs were unchanged and all
remained running. Public health endpoints and the onboarding page returned 200;
anonymous schema access returned 401. The updated local CLI's authenticated
`vc check` returned `compatible: true`. No schema or cloud resources changed.

## Confidential-dotfile onboarding release

This earlier release at https://vault.pocketcontext.com ran
`ceba5620da95bfaaf74f32e25790576374496b07`, manifest
`sha256:f45fdf47d6a670763ea100f82b7863334301714deaab298c6d889cf5c8a33291`.
The standalone launcher pins `8f09fd8b75ad29e1663ab98533a7a971b386fd95`.
The public page introduces confidential dotfiles, encrypted path labels and a
synthetic `.env` save/restore walkthrough. Both setup paths install the skill with
`npx skills` and copy its launcher onto PATH. Vault operations remain in the CLI.

[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37140997192)
and [container recovery gates and publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37140997509)
passed, including AMD64/ARM64 publication. Local pinned-server validation,
copied-launcher tests and final ARM64 container config/smoke/populated restore
checks also passed.

The installed wrapper hash matched reviewed source. The local dedicated deployment
key was rejected; existing operator SSH access invoked the same locked graceful-stop
wrapper successfully. No deployment keys were changed. Production matched the
exact revision, manifest and all three source asset hashes, with one running writer,
one CPU, 512 MiB, the unchanged server pin and automatic updates disabled.
All fourteen sibling container IDs and running states were unchanged.

Public health, CSP/security headers, anonymous schema rejection and private source
path denial passed. CSS/JS matched exactly; HTML matched after accounting for
Cloudflare's existing Rocket Loader transformation. Live desktop/mobile, 320-pixel
layout, keyboard navigation, method switching, clipboard success/fallback and
no-JavaScript disclosures passed. No application data, schema or cloud resources
changed. Real Google browser login and independent security review remain separate
outstanding checks.

## Management-only agent skill release

This earlier release ran `33b9324296230b922631d6d4cb1835c682bf8414`, manifest
`sha256:1c0951fc739ee31942c3629d5a9787e61bf85e305e6dc69b4cf92d913dfbc28f`.
The launcher pins `5e2562624d9302d77eb2d9a01b8c7cf462fefb19`. The skill now
permits file management and metadata inspection only; agents must not inspect
contents, including source/restored files or content-derived summaries. Private
terminal viewing remains available to users. This is an instruction boundary;
CLI decryption capabilities and the trusted-host model are unchanged.

[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37136729795)
and [container recovery gates and publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37136729936)
passed. Both copied-launcher release tests also passed locally. Installed skill
instructions and the local PATH launcher were refreshed; authenticated `vc check`
returned `compatible: true`. No unlock session was stopped.

The installed wrapper hash matched the reviewed source. Existing operator SSH
access invoked that locked graceful-stop wrapper. Post-update checks matched the
exact revision and digest, one running writer, unchanged server pin, one CPU,
512 MiB and automatic updates disabled. All sibling container IDs were unchanged;
all public health endpoints returned HTTP 200, and anonymous VaultContext schema
access returned 401. No cloud resources, schema or runtime behavior changed.

## Path labels and document archive release

This earlier release ran `6a9fa4b614102189391b79a58a75ec372ff117ad`, manifest
`sha256:65daf0042393c7c1263c08ab16db312c82a9fd70288a852553a019e2dbb778bb`.
The launcher pins application/client `7619d0c`, preserving literal supplied path
labels and adding archive/unarchive with a separate archive-state revision.
Existing documents migrate active without changing their names or signed versions.
Encrypted export v2 records archive status; updated clients still read v1 exports.

[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37135572376)
and [container checks, populated recovery and image publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37135572572)
passed. See [local validation](docs/validation.md#path-labels-and-document-archive-release--2026-10-03).
The package-only staging commit skipped CI while its launcher still pinned the old
schema; all release gates ran on the final matching launcher commit before deployment.

The existing restricted-key wrapper completed the locked graceful-stop update.
Post-deployment checks matched the exact revision and manifest, one running writer,
unchanged server pin, one CPU, 512 MiB and automatic updates disabled. Every sibling
container ID was unchanged. VaultContext and sibling public health endpoints
returned HTTP 200; anonymous schema access returned 401. The updated local PATH
launcher authenticated normally and `vc check` returned `compatible: true`.
Installed skill instructions were refreshed. No active unlock session was stopped;
run `vc lock` then `vc unlock` to replace a session using older client code.
No cloud resources or generic server changes were needed.

## Cat release update

This earlier release ran `91ac202ba5b7cced0323c8a1009f6d8e1c26bd35`, manifest
`sha256:350a85961692a30e0fe0c2d2da24ad1085500591b9f2d6b23ddc357abff5ab4c`.
The launcher pins client `81559e944ceb01896ca0fe76e311c33bcb5ff296`, adding
`vc cat DOCUMENT_ID [--version VERSION_ID]`. The local PATH launcher and installed
skill documentation were updated. Restart an already-unlocked session with
`vc lock` then `vc unlock` to load the new client code.

[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37133894422)
and [container checks, populated recovery and image publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37133894691)
passed. See [cat validation](docs/validation.md#cat-release--2026-10-03) for exact-byte,
authorization, integrity and pipe coverage, plus the corrected cache-test assertion.

The existing verified restricted-key wrapper completed a locked graceful-stop
update. Post-deployment inspection matched the exact image revision and digest,
one running writer, the unchanged server pin, one CPU, 512 MiB and automatic
updates disabled. All sibling container IDs were unchanged. VaultContext and all
sibling public health endpoints returned HTTP 200; anonymous schema access
returned 401. No server behavior, schema or cloud resources changed.

## CLI help release update

This earlier release ran `30b4dedbf4a529b010146c5377151457d0b5579a`, with manifest
`sha256:1bf88095d05cec887b609891b58226791b180c14b058d8fe556d17e1346dfe80`.
The standalone launcher pins help implementation
`b7845ac443f64a1e9b513a04e90545cc7fe3b06d`; the local PATH launcher was updated.
[Application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37132558839)
and [container gates and publication](https://github.com/pocketcontext/vaultcontext/actions/runs/37132558990)
passed, including copied-launcher session tests and populated backup recovery.
See [local validation](docs/validation.md#cli-help-release--2026-10-03).

The existing restricted deployment key invoked the verified locked graceful-stop
wrapper. Post-update inspection matched the exact revision and manifest, one
running writer, unchanged server pin, one CPU, 512 MiB and disabled automatic
updates. VaultContext and all sibling public health endpoints returned HTTP 200;
anonymous schema access returned 401. NotifyContext changed independently before
the VaultContext update (15:20:53 versus 15:22:56 UTC); all other sibling container
IDs were unchanged and every container remained running. No schema, application
behavior or cloud resources changed in this release.

## Initial deployment record

Live origin: https://vault.pocketcontext.com. Interface: portable CLI/skill; no
browser frontend. The public repository is
https://github.com/pocketcontext/vaultcontext and the public image is
`ghcr.io/pocketcontext/vaultcontext`.

- Source: `4faa856ef369ae3064e16521ef9e7bb928061e3d`.
- PocketContext pin: `a92b0de5e1b66b6d3b6135b90092d2d6da5f7cc8`.
- Published ARM64/AMD64 manifest:
  `sha256:be53c2bcbce79fe0f0dfc825ef7af98f8016076075e08aa4d97eaf33b0096db4`.
- [Publication and test gates](https://github.com/pocketcontext/vaultcontext/actions/runs/37128418154)
  passed. Earlier [application validation](https://github.com/pocketcontext/vaultcontext/actions/runs/37128274141)
  and [container checks](https://github.com/pocketcontext/vaultcontext/actions/runs/37128274389)
  also passed.
- Existing ONCE host: `130.61.106.194`, ARM64. One CPU, 512 MiB, persistent
  `/storage`, TLS enabled and automatic updates disabled.

Anonymous registry token/manifest access and a complete pull using an empty Docker
credential directory on the host succeeded. The first deployment used the immutable
manifest digest. A subsequent restricted-key update to `latest` pulled that same
digest; serving revision and resource settings were verified afterward.

## Configuration and isolation

The scaffold is `once-pocketcontext/colors.yml`. Its private `.envrc.private`
contains all app-specific environment settings and the dedicated deployment key
path, is ignored by Git and has mode 0600. At the user's request, operator
credentials reference the existing shared DealContext credentials and R2 uses
the existing account's EU S3 endpoint. Google uses a separate client with
Workspace domain `pocketcontext.com`.

The dedicated bucket is `vaultcontext-backup`, prefix
`once-pocketcontext/vaultcontext`, region `auto`. Authenticated object
write/read/list/delete passed, disposable probes were removed, and an unsigned
S3 read was rejected. Provider-level public-domain settings were not inspected.

A saved, reviewed OpenTofu plan added exactly the proxied A record
`vault.pocketcontext.com` at the existing host and updated the existing DNS state.
No full scaffold convergence, compute replacement or SMTP changes occurred.
Initial deployment copied the existing CRM SMTP settings privately. Sibling
container IDs/running states and deployment-key entries were preserved.

## Verified service and backups

HTTPS `/up`, operator authentication, HTTPS app origin, SMTP enablement, rate
limits, trusted proxy, default `users`, and the configured Google client passed.
Anonymous schema/SQL and direct signup were rejected. No `agents` collection or
ordinary users were created. Every sibling app/demo/website health check returned
HTTP 200 after deployment.

Verified exactly one running VaultContext container, persistent storage, disabled
automatic updates and the expected source revision. The startup complete snapshot
uploaded successfully and nonempty Litestream LTX objects were present. The live
complete snapshot was downloaded, checksum-verified and restored into an isolated
temporary directory; SQLite integrity passed. This took approximately 0.41 seconds
for the initial empty application database. No replica writer was started and
the restored files were removed. This is not a populated production recovery-time
measurement; populated encrypted-file recovery passed in isolated CI/local tests.

Complete snapshots run after startup, after each 3600-second interval following
upload, and during graceful shutdown. Recovery uses the latest complete snapshot,
including ciphertext files; a newer database-only replica is insufficient. Monitor
backup age and size as usage grows. Retention remains an operator decision.

## Updates and recovery

The dedicated key `~/.ssh/vaultcontext-deploy` invokes only
`sudo -n /usr/local/sbin/deploy-vaultcontext`, with SSH restrictions and no-argument
sudo permission. The root-owned wrapper uses `/run/lock/deploy-vaultcontext.lock`,
pulls the fixed image, gracefully stops the sole writer and updates only this
application. A real restricted-key update passed. Temporary deployment scripts
were removed. The trusted host key was taken from the existing DealContext GitHub
deployment environment and matched on connection.

GitHub environment `once-pocketcontext` contains the dedicated SSH secret and
`SERVER_IP`, `SERVER_USER`, `SSH_KNOWN_HOSTS`; `COLORS_PROFILE=once-pocketcontext`
is configured. `VAULTCONTEXT_PUBLISH=true` enables tested image publication. The
workflow has no deployment job; later image publication does not update production.
Do not use ordinary ONCE rolling updates or enable automatic updates.

Stop the production writer before rollback or restoration. Use a prior image only
with a compatible schema; otherwise restore a verified complete snapshot with a
deliberate replica strategy. Never run a restored writer against the live replica
beside production. Backups include sensitive auth settings and must remain private.

## Remaining user verification

Real Google browser login and first-user JIT remain unverified. Registered redirects
must be `http://127.0.0.1:8765/callback` and
`https://vault.pocketcontext.com/api/oauth2-redirect`. Server/provider configuration
checks and synthetic OAuth tests do not prove Google's console audience/redirect
settings. No independent security audit was performed. See
[validation and limitations](docs/validation.md).
