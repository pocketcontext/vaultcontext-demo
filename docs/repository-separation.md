# Dedicated demo repository

`pocketcontext/vaultcontext-demo` owns the public disposable demo, with `main` as
its release branch. It retains the full ancestry of the original
`pocketcontext/vaultcontext` demo branch. The client command and skill remain
`vaultcontext`; repository ownership does not rename the user interface.

The demo has independent CI, package source, image publication and deployment
controls. Images use `ghcr.io/pocketcontext/vaultcontext-demo`; the skill installs
from `https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext`.
The repository initially retains its complete application, hooks and client.
Core VaultContext remains independently owned by `pocketcontext/vaultcontext`.

## Cutover status and boundaries

The repository cutover completed on 10 October 2026. The existing demo hostname,
Google OAuth client, ONCE volume, private object stores and three databases were
preserved. No cloud resources were created and no data was initialized.

The old repository deployed compatibility bridge `f7dcb00` through
[its validated release](https://github.com/pocketcontext/vaultcontext/actions/runs/38029197994).
The dedicated repository then deployed revision `9d45c1c`, image
`ghcr.io/pocketcontext/vaultcontext-demo@sha256:9096267f7855a4750fe9aa22999786fd16f1eb92c754cebec6382597f567e834`,
through [its native validation and CD run](https://github.com/pocketcontext/vaultcontext-demo/actions/runs/38029377538).
The release receipt and public asset/installation checks passed. The old demo
publishing/CD flags are disabled, its environment deployment secret was removed,
and the dedicated environment is restricted to `main`. Later normal releases
may supersede this initial cutover image.

For any future repository migration, pause the old release path before enabling the new one. Mirror the
pinned base images into the new namespace and verify anonymous access. Configure
the dedicated GitHub environment for `main`, and pass native application,
container recovery and copied-launcher checks before deployment. The old running
supervisor accepts only the original image namespace; a reviewed transition
release must admit the new namespace before the dedicated image can be prepared.
The dedicated runtime and dispatcher accept only the new namespace. Verify the
exact digest/revision receipt, public assets and installation contract after the
cutover. Follow [CI and deployment](ci-and-deployment.md) and
[demo operations](demo-operations.md).

[DEPLOYMENT.md](../DEPLOYMENT.md) preserves historical core VaultContext evidence.
It does not describe a deployment from this repository.

## Shared-code review policy

Review security and client fixes in both repositories and explicitly adopt those
that apply. Record the source commit, selected changes, compatibility impact and
validation in the adopting change. Do not merge demo enrollment, daily deletion,
contact retention or restricted-sharing policy into core as incidental fixes.
Each repository publishes and verifies its own client pins and server releases.

The preserved branch contains these upstream review candidates:

| Source commits | Candidate and review boundary |
| --- | --- |
| `eadfcf6`, `1164339` | Signed identity-bundle replacement: client signatures and server verification bind the account, expected revision and replacement bundle to the existing signing identity. Also review fingerprint-directory ownership and permission checks from `eadfcf6`. Separate these from demo session/enrollment behavior. Coordinate client/server adoption because older clients cannot perform signed replacement. |
| `8b8b177` | Check access and existing identity before prompting during `init`; preserve existing identities and useful enrollment/access failures. Review generic initialization safety separately from demo-only errors. |
| `78e0308` | Google-only CLI login removes password login and the old `--google` flag. This is an authentication/product policy choice, not an automatic security backport. Core adoption requires its own compatibility decision and tests. |

Core `64ac048` subsequently adopted the signed replacement, initialization and
fingerprint-file protections from the first two rows. See the
[11 October upstream review](upstream-review-2026-10-11.md) for the demo's selective
adoption and exclusions of subsequent core changes. Google-only CLI login remains
a separate policy choice.
Changes to identity signatures require coordinated client/backend tests, including
`tests/identity_rewrap.py`. Initialization and login changes need client, OAuth,
portable-launcher and integration validation in the adopting repository.
