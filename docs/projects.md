# Project files (`.edbproj`)

A project file lets one analyst hand a whole working set - the databases, the open tabs, the layout and notes -
to another analyst on another computer, with proof that nothing changed on the way, proof of who made it, and
(optionally) nobody else able to read it.

## Container

A project is a ZIP archive:

| Member | Content |
|---|---|
| `manifest.json` | the project: name, notes, creation time, author (user / host / platform), EDB Explorer version, `evidence_root`, the databases (`id`, original `path`, `name`, `size`, `sha256`, `kind`, `relative` path, `embedded` member name or `null`) and the `workspace` (open tabs, current tab, dock layout, theme). Stored as `manifest.enc` when the project is encrypted. |
| `evidence/NNN_<file>` | the database files themselves when *Embed the evidence files* was chosen (encrypted when a password is set). |
| `crypto.json` | present only when encrypted: cipher (`AES-256-GCM`), KDF (`scrypt`, n = 2^15, r = 8, p = 1) and the random 16-byte salt. |
| `hashes.json` | the **integrity hash map**: `members` = SHA-256 of every other member exactly as stored (encrypted bytes included), `evidence` = SHA-256 of every original database file, plus creation time and app version. |
| `signature.json` | `Ed25519` signature over the exact bytes of `hashes.json`, the signer's raw public key (base64), its fingerprint and the signer's name / host / time. |

## Integrity

`inspect_project()` recomputes the SHA-256 of every member and compares it with `hashes.json`; a member that
differs, is missing, or is not listed makes the check fail and the import refuses to proceed (the CLI has
`--force`, the GUI asks). Because the hash map covers the *stored* bytes, an encrypted project can be verified
without its password.

After import every evidence file is hashed again and compared with the `evidence` entry of the hash map - an
embedded file that does not match is deleted, a file found on the other computer only counts when its hash
matches. Evidence that is not embedded is looked for in this order: the original absolute path, then
`<search folder>/<relative path>`, then `<search folder>/<name>`, then any file of that name anywhere below the
search folder - always confirmed by SHA-256.

## Signing

The first export creates `signing_key.pem` (Ed25519, PKCS8) and `signing_key.json` (name, host) in the config
directory - `~/.config/edb-explorer/` on Linux/macOS, `%APPDATA%\EDB Explorer\` on Windows, or
`$EDB_EXPLORER_CONFIG_DIR`. The signature is computed over `hashes.json`, which in turn pins every member, so it
covers the whole file. A re-hashed forgery fails the signature; a swapped public key fails too.

The importer shows the signer and the **fingerprint** (first 128 bits of SHA-256 of the public key, as
`xxxx:xxxx:…`). Compare it with the sender over another channel, then *Trust this signer* (or
`edb-explorer project trust <fingerprint> --name …`); trusted fingerprints are kept in `trusted_signers.json`
and later projects from the same key are shown as *trusted (name)*.

## Confidentiality

With a password the manifest and every embedded file are encrypted with AES-256-GCM using a key derived by
scrypt from the password and a random salt. Large files are encrypted in 4 MiB chunks: each chunk uses the
nonce `prefix || chunk index` and the member name and index as associated data, so chunks cannot be dropped,
reordered or moved between members without failing authentication. A wrong password fails on the first chunk.
The encryption happens *before* hashing and signing, so integrity and signature checks never need the password.

## CLI

```bash
edb-explorer project export case.edbproj ntds.dit Security.evtx --name "Case 42" --notes "…" \
    --embed --ask-password --signer "J. Doe"
edb-explorer project info case.edbproj [--password …] [--json]
edb-explorer project import case.edbproj --dest ~/cases --search /mnt/evidence --ask-password [--gui] [--force]
edb-explorer project trust [<fingerprint> --name "…" | --forget]
```
