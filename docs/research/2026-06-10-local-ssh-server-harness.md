# Local SSH Server Harness Research

Date: 2026-06-10

## Decision

Use AsyncSSH for automated loopback SSH/SFTP spool tests, installed through the
optional `ssh` extra. Twisted Conch remains viable, but it is not the best
default for this project because its SFTP server surface needs more
Twisted-specific fixture code and is less directly documented for this use case.

The SSH harness is for event-spool transport only. Queue mutation from remote
agents remains governed by `docs/task-ingest-command-contract.md` and should be
tested separately from event files.

## Options

AsyncSSH is the recommended test dependency. Its current documentation describes
Python 3.10+ SSHv2 client and server support, SFTP/SCP client and server
support, `start_sftp_client()`, and `listen(..., sftp_factory=True)`. That
matches this repository's Python requirement and lets tests start an in-process
loopback server with a chrooted SFTP root. The main caveat is its
`EPL-2.0 OR GPL-2.0-or-later` license, so it should stay optional unless the
project later accepts it as a runtime dependency.

Twisted Conch is a reasonable alternative when a Twisted stack is already in
use. It has an MIT license and SFTP protocol support, but its stable docs expose
less concise server-fixture guidance for this use case. Adopting it here would
add a larger framework dependency and more project-specific SFTP adapter code.

An OpenSSH subprocess is the most production-like integration check, but it is
not a good default CI fixture. It depends on `sshd` availability, generated host
and user keys, temporary config, port management, and platform-specific
hardening. Root-owned chroot requirements also make unprivileged CI harder.

No true SSH server is acceptable only as a fallback. The existing filesystem
tests cover file filtering, atomic local publish, idempotent skips, conflicts,
and ingest movement, but they do not cover SSH URI parsing, host-key policy,
authentication, SFTP listing, or SFTP read failures.

## Test Boundary

Automated SSH/SFTP tests should cover:

- complete event JSON listing and read through SFTP;
- `.partial`, `.part`, and `.tmp` skip behavior;
- dry-run output without local mutation;
- real pull into the local inbox through a temporary non-JSON file;
- ingest movement to `done` or `error`;
- repeat-pull idempotency through `skip_existing`, `skip_done`, or
  `skip_error`.

Command request/response tests should remain separate because they need lease
tokens, idempotency keys, processor-owned SQLite mutation, and durable response
files. An event-spool SFTP fixture is not end-to-end remote queue mutation
coverage.

## Sources

- AsyncSSH documentation: https://asyncssh.readthedocs.io/en/latest/
- Twisted Conch documentation: https://docs.twisted.org/en/stable/conch/
- OpenSSH `sshd_config` manual: https://man.openbsd.org/sshd_config
