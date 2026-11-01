# Data and trust model

The application server stores encrypted identities, per-user public encryption/signing keys, vault metadata ciphertext, memberships, recipient key envelopes, invitations, documents, immutable file versions, ciphertext chunks and metadata-only audit events. Exact field/API definitions are in `docs/api-contract.md`.

Google authenticates default PocketBase `users`; it does not unlock a vault. Direct signup remains blocked. Verified Workspace JIT admits only ordinary identities. Disable an application identity to revoke sessions; Google suspension alone does not revoke existing application tokens. Operators do not gain decryption keys through application authority.

The client's passphrase-derived key protects its private-key bundle. X25519 recipient encryption and Ed25519 signatures protect distribution of independent vault keys. A versioned AEAD format binds ciphertext to its context. Authenticated encryption does not by itself detect server rollback to a previously valid snapshot; server revision checks address ordinary concurrent changes, not malicious history replay.

Initial public keys are immutable. Recipient fingerprints require independent verification before sharing and are pinned locally. Members are owner/editor/reader; only the owner manages grants. New members receive every retained epoch needed for vault history. Invitations convey no ordinary vault access until acceptance. A reader can still copy plaintext or keys; roles constrain application writes, not redistribution.

Identity-bundle replacement now requires a server-verified Ed25519 signature from
the immutable identity key, bound to the account, expected revision, replacement
bundle and operation. An application token alone cannot overwrite the bundle.
The actor-private audit event contains no bundle or passphrase. An unlocked
same-user session still has signing authority and may change a forgotten
passphrase outside demo mode; its expiry does not recall copied plaintext or
keys. The public demo rejects passphrase changes entirely. Existing signed file
versions authenticate their authors, not independently verifiable historical
editor grants from a malicious server; current membership is not a substitute
for historical grant evidence.

In explicit demo mode, all verified Google accounts are eligible, directory and
identity rows are self-only, and sharing is unavailable. Demo data is scoped to
a persisted UTC generation; API access and realtime delivery fail closed after
midnight. Separate contact preferences and security metadata follow their own
retention policy and do not carry vault contents or names. See demo operations.

Revocation immediately removes authorization and freezes publication. An unlocked owner rotates the current vault key and publishes envelopes for exactly the remaining authorized recipients. Rotation must account for outstanding invitations and stale clients. Previously disclosed secrets remain disclosed.

The local host and an unlocked agent are trusted. File contents are untrusted input, never instructions. Session keys are memory-only; Python/runtime copies, privileged processes and host swap can defeat perfect memory erasure. Limit session lifetimes and disable core dumps where supported. Avoid returning plaintext or decrypted names beyond the requested task.

The local memory session starts a fresh interpreter rather than continuing after a Python fork. The foreground client verifies the identity before transferring only that identity, account and request configuration over a private inherited socket. Passphrases and encrypted identity bundles do not cross this startup channel. Both processes disable core dumps; standard streams are detached in the worker. A bounded readiness handshake precedes unlock success. The process uses the current interpreter without resolving virtual-environment symlinks.

Workers bind authentication to the immutable account ID verified at unlock. Cached
login replacement and renewal to a different account fail before a vault request.
Missing login never starts authentication implicitly; renewal leaves the fixed
unlock deadline and demo generation/reset limit unchanged. This client-side
safeguard requires updating installed launchers and explicitly locking/unlocking.

A persistent, empty, mode-0600 lifecycle lock serializes unlock, lock and logout per server/account. Session sockets are mode 0600 in an owned mode-0700 directory, and both sides check native peer credentials. Recovery removes only an unchanged, owned socket after connection refusal; timeouts and unsafe paths fail closed. An exiting worker removes only its own socket inode. Abrupt termination may leave a stale socket, but never a key-bearing file.

Original file bytes are encrypted into immutable protected-file chunks. SQL exports ciphertext storage filenames and ciphertext checksums, never materializing large file payloads into filtered snapshots. Server recovery combines a consistent database replica with retained primary S3 objects and verifies ciphertext checksums before serving restored state. Other actual file fields, including avatars, are checked for complete readable responses but have no authoritative stored checksum. Such backups are server-state copies, not client private-key caches. An ordinary data export excludes the identity bundle and uses a separate archive passphrase. There are no recovery keys; preserving backup bytes cannot replace a lost decryption passphrase. An optionally enrolled macOS Keychain credential is a retained copy of that passphrase, so an enrolled Mac may still unlock after the user forgets it.

New file versions include `plaintext_sha256` inside their encrypted metadata, covered by the writer's signed manifest. This plaintext checksum is never stored in clear server fields, returned by metadata listings or printed by comparison. It is distinct from public ciphertext checksums used to verify transport and backup integrity. Encrypting it prevents the server from using a plaintext checksum to recognize known files or test guesses about their contents.

An unlocked client can compare an explicit local path with a verified version's encrypted checksum without downloading its file chunks. The result identifies the document/version and exposes only equality and the comparison method. Older versions without this field fall back to verified in-memory content download; immutable versions are not migrated. Invalid present checksum fields fail closed. Normal content reads verify the plaintext checksum when present. A metadata-only equality check trusts the authenticated writer's checksum and does not validate availability or integrity of the stored file chunks; complete backup/restore checks remain necessary.

Document archive state is reversible operational metadata, separate from the signed content revision. Owners and editors archive/unarchive with both content and archive revision checks. Archived documents retain all versions and permissions but reject saves until unarchived. Archive transitions are audited; exports and complete backups retain archived documents.

## Optional macOS Keychain unlock

Explicit enrollment validates the entered passphrase against the authenticated account's server bundle before saving it in a non-synchronizing Data Protection Keychain item. The item is scoped to the server origin and immutable account ID. It stores only the passphrase; the encrypted identity bundle remains server-only, and decrypted keys remain in memory. Linux and ordinary terminal unlock do not depend on Keychain.

A signed native helper retrieves the secret with Apple's `userPresence` access control. Touch ID or a macOS credential may satisfy this requirement; this is not a guarantee that no password prompt will appear. Authentication gates retrieval for a new session, not each file operation. The same-user CLI and agents retain the existing unlocked-session authority. The host, signing identity and installed helper are part of the trusted computing base. Secrets cross private process pipes in memory, never shell arguments, environment variables or files.

`unlock --keychain` fails on cancellation, unavailable credentials or helper errors without silently falling back to a terminal passphrase. `lock`, expiry and `logout` do not delete enrollment. `keychain-forget` removes the current server/account's saved credential; use `lock` separately to end any active memory session. Changing the vault passphrase does not update the enrolled credential; enroll again with the new passphrase or forget the stale credential. Keychain removal cannot recall a previously disclosed passphrase. See [macOS setup](macos-keychain.md) for installation and on-device checks.
