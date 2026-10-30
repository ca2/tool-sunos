# Shared SSH agent

Source `bin/defer_ssh_agent` to export a usable `SSH_AUTH_SOCK`. `_ambient`
invokes it for shell startup, including noninteractive SSH/build shells.

The helper validates the inherited socket first (including forwarded agents),
then tries the socket saved in `~/.cache/ca2/ssh-agent/socket`. Exit status 1
from `ssh-add -l` means an available, empty agent. The cache is plain socket
path data; it is never sourced as shell code. Switching sockets clears a stale
`SSH_AGENT_PID`; agent availability is determined by the socket, not a PID.

Only an interactive shell with terminal input and error output may create an
agent. Creation uses an atomic directory lock and rechecks the cache under the
lock. The socket file is published atomically with mode 600 inside a mode 700
directory. An interactive terminal loads `~/.ssh/id_auth` only when the chosen
agent has no identities. Additional keys can be loaded explicitly with
`ssh-add`. Noninteractive shells neither create agents nor load keys.

Agents persist across terminal exits. A dead agent or a reboot leaves a stale
socket that the next interactive terminal replaces. Lock contention waits at
most four seconds per invocation. Normal exits/signals remove the lock; after
a forced kill or crash, if no helper invocation is still active, remove a
leftover empty `~/.cache/ca2/ssh-agent/lock` directory with `rmdir` and retry.
The helper never kills existing agents or starts a competing agent without
holding the lock.

Solaris integration tests use isolated temporary homes and real SSH agents:

```sh
python3 tests/test_defer_ssh_agent.py
```

Tests cover concurrent creation, inherited-agent precedence, noninteractive
reuse, stale sockets, cache contents as data, bounded lock waits, private file
permissions, and interactive-only key loading. Test agents are stopped during
cleanup.
