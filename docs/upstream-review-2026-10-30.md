# Upstream review — 30 October 2026

Scope: the PocketContext server pin only. The demo advances from
`2fb371c3466c7a5b65adbd6cf95309b46d27c4ee` to
`416841c4d1225ddc464ce0b1e787a797d5b8d7e8` (server v0.9.4, PocketBase v0.41.1),
matching core VaultContext `b8a19f82c8ec3c2072250d9568cfd47dcb3152cb`. Other
core changes since the 11 October review are not reviewed here.

Server changes after the previous pin: `4b03d29` reuses each requester's
synchronous snapshot until a source-database commit (default on); `20843b7`
adds snapshot indexes and opt-in `trackChanges`; `934a63a` adds opt-in
background snapshots; `279b04b` upgrades PocketBase 0.40.4 to 0.41.1 (JS hook
and migration API unchanged); the rest is installer/release tooling. The demo
keeps synchronous mode, does not configure `trackChanges` or background
snapshots, and keeps its existing `maxConcurrentPerRequester: 1`. Its filters use
no date/time functions, so reuse applies; the stale-generation and enrollment
guard runs as request middleware before any snapshot is served.

| Core commit | Demo disposition |
| --- | --- |
| `b8a19f8` | Adopt the server pin and the README description, adapted. Exclude `tests/snapshot_freshness.py`: it exercises invitations, acceptance and revocation, which the demo rejects. New `tests/demo_snapshot_freshness.py` covers reuse, onboarding, saves, another visitor's writes, disablement, stale generation, restart and reset against the demo's Google-only, sharing-disabled lifecycle. |

The compatibility declaration adopts the new server/migration fingerprint
`b4602755fdc27b256c2912acf0cdf03b94dfe8a33975973269fed76a55313af7` and retains
predecessors `76472c21…` (pin `2fb371c`) and `761af213…`. Migrations are unchanged.
