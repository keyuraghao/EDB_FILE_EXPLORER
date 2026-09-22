# Security policy

EDB Explorer only ever opens database files for reading; it never writes to, repairs or
modifies an ESE file. Exports and reports are written to paths you choose.

## MCP server

The MCP server gives an AI agent the ability to open and read files on the host. When you run
it, restrict what the agent may open:

```bash
edb-explorer mcp --allow /cases/2026-001/evidence
# or
EDB_EXPLORER_ALLOWED_PATHS=/cases/2026-001/evidence edb-explorer mcp
```

Only stdio transport is enabled by default; HTTP transports bind to `127.0.0.1` unless you
pass `--host`. Put a reverse proxy with authentication in front of it before exposing it on a
network. The Docker image restricts opening to the `/evidence` mount.

## Project files and portable builds

`.edbproj` project files carry a SHA-256 hash map of every member and an Ed25519 signature over it;
verify the signer's fingerprint through another channel before trusting it (`edb-explorer project trust`).
A password encrypts the manifest and embedded evidence with AES-256-GCM (scrypt-derived key). Signing
keys live in the config directory (`~/.config/edb-explorer`, `%APPDATA%\EDB Explorer`) with mode 600;
portable builds keep them in `./data/config` next to the executable - protect that folder accordingly.

## Reporting a vulnerability

Please open a private security advisory on GitHub
(*Security* → *Report a vulnerability*) rather than a public issue. Include the version,
platform and a minimal reproduction. You should receive a response within 7 days.
