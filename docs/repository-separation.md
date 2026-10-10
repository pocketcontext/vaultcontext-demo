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

The repository cutover is in progress. This document records intended ownership
and release requirements, not successful deployment evidence. The existing demo
hostname, Google OAuth client, ONCE volume, private object stores and three
databases must be preserved. Separation requires no new cloud resources or data
initialization.

Pause old-repository demo CD before enabling the new release path. Mirror the
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

These are review candidates, not claims that core VaultContext has adopted them.
Changes to identity signatures require coordinated client/backend tests, including
`tests/identity_rewrap.py`. Initialization and login changes need client, OAuth,
portable-launcher and integration validation in the adopting repository.
