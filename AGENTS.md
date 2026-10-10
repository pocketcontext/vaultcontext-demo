# VaultContext Demo

Read README.md and docs/data-model.md before changes. This app stores any file type as opaque encrypted bytes, with personal and explicitly shared vaults. Keep domain logic here and PocketContext application-independent. Use default `users`, verified Workspace Google JIT, filtered SQL reads and REST writes. No plaintext file names, content, passphrases or private keys in server records, logs, traces or tests; fixtures are synthetic.

User private-key bundles persist encrypted only on the server. Download and unlock in memory only. No local bundle cache, recovery key or key-bearing export. Standalone file exports use a separately entered archive passphrase. Never accept unlock secrets in command arguments or environment variables. Protect local sessions and document the trusted host/agent boundary and memory-erasure limitations.

Preserve independent SQL/REST/realtime authorization, verified recipient fingerprints, signed envelopes/manifests, immutable versions, revision conflicts, full-history sharing and key-generation rotation after revocation. Never source or execute restored files. Restore exact bytes with restrictive permissions and explicit overwrite protection.

Run tests on the pinned server with isolated temporary databases; never use local pb_data. Follow README validation commands, including crypto, client, backend, auth, OAuth, deployment and complete populated restore checks. Real provider/browser verification and independent security review remain release considerations. Do not provision, publish or deploy without authorization.

This is the dedicated `pocketcontext/vaultcontext-demo` repository; `main` releases only the disposable demo. Keep the CLI and skill name `vaultcontext`. Preserve core ancestry and follow README upstream maintenance: review shared security/client fixes in both directions, retain source commit references, and validate each application independently. Core production deployment belongs to the separate VaultContext repository.
