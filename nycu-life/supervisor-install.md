# Coordinated supervisor and unlock-helper installation draft

The transaction CLI prints a plan and accepts no apply option. The separately
reviewed management entrypoint must provide the
native adapter, private empty operation directory, exact protected-storage
snapshots and the existing controller's freshly collected complete Mac queue
proof. No new service, runner pool, label, environment setting or crash replayer
is introduced. `maintenance_recovery.py` remains unchanged.

The exact source pair is the PR7 supervisor `a31b4ac6…` and PR3 helper
`a644e6ed…`. The reviewed one-shot installation required the then-live supervisor
`bcb39309…`, unchanged plist `5cf9d1e8…`, GOLDEN `macos-ci` and initial PID99763,
started `Wed Oct 28 10:02:52 2026`. Both PID and start are checked. A changed
initial identity requires a new reviewed authorization; do not blindly rerun an
operation or rewrite its old receipts. All full hashes are in `supervisor_install.py`.

Under the same `toolchain-install.lock`, prepare private old-source backups and
both complete replacement/rollback pairs, preserving an absent helper as absent.
Record their hashes, modes, file identities, queue/control/storage proof and
rollback authorization before exclusively creating this operation's drain.
Unconsumed rollback copies stay in the private operation evidence on the same
filesystem; cleanup of unused new staging must not remove the old pair before
the final release checks have committed admission.
Require all VMs stopped, no job clone, and a fresh drained/idle original process.
Boot out only the exact known service, verify it and its old process absent,
replace each complete file atomically while the service is unloaded, then
bootstrap the **unchanged** plist. The two renames are not one filesystem-wide
atomic pair; the unloaded service plus persistent drain provides the boundary.

The new service must have a fresh PID/start identity, expected UID/process group,
the exact new on-disk pair authority and the unchanged plist/GOLDEN. A newly
appended startup-drain line and only idle sleep children establish drained
health. Fresh queue, full VM/storage, source/lock and exact owned-marker checks
remain mandatory before removing the drain. Existing bcb-pinned maintenance
proofs are not silently rewritten: subsequent maintenance needs a separately
reviewed proof for the newly observed process and source authority.

On a caught failure, keep the original nonzero code. If the original service
never stopped and its pair is intact, verify and retain that process. Otherwise
stop only a proven fresh replacement under this operation's pair/plist
authority, restore the captured old pair/absent-helper state, and bootstrap the
old service under a **fresh** verified PID/start identity. The rollback therefore
does not claim the old PID survived a restart. Only after healthy rollback and
all guards pass may it remove its exact owned drain. Optional later audit-write
ENOSPC is reported; the immutable pre-mutation authorization and pre-staged old
files remain mandatory. Failure to restore files, bootstrap or establish safe
ownership leaves the drain and a nonzero unresolved result.

The installer neither opens credential files nor invokes keychain unlock. Once
it releases its drain, however, the existing supervisor's normal startup may
perform its already-authorized private-PTY unlock and admit queued work. This
operation cannot claim zero keychain access across that resumed routine. It
does not modify VM images, plist/environment, keychain settings, GUI or passwords.

Verified drain release is the admission commit point. If only the final outcome
audit fails afterward, report nonzero with `installed` and `admission_restored`
still true. Do not restart a committed service when a new job may already have
been admitted. The earlier durable installed/drained receipt and release intent
remain available; this is not a claim that a missing final receipt was saved.

SIGKILL, power loss and interpreter failure cannot execute rollback. A native
bootout/bootstrap request timeout also has uncertain launchd side effects and
is retained for explicit review, not blindly replayed. The request is marked
pending before dispatch. TERM/HUP, keyboard interruption, SystemExit, timeout or
other exceptional exits keep that uncertainty; reaping the CLI does not prove
the daemon outcome. A normally returned native exit code resolves only that
request, preserving any earlier uncertainty; a signal-killed CLI remains unknown.
Durable intents and
private backups aid that review; they are not automatic crash recovery.
A disposable Mac HOST file-only canary passed directory fsync, same-directory
atomic replacement and restoration from prepared synthetic data on its APFS
working filesystem. It removed its own temporary files and did not run this
installer, launchctl, a VM or keychain unlock. The later native rollback result
is recorded below. This primitive result alone does not establish deployment
or JIT runner acceptance.

The new offline tests exercise the transaction through Facts and selected actual
file/identity primitives with isolated temporary files or native-command mocks.
They do not install a HOST service, unlock a keychain, execute launchctl, touch
VMs, or establish native deployment/JIT acceptance.

```sh
python3 nycu-life/supervisor_install.py
python3 nycu-life/tests/test_supervisor_install.py -v
```

## Reviewed caller preparation

`supervisor_install_entry.py` is a separate HOST caller draft. Its default mode
prints a plan. Its fixed main/CI pins must be successful and still current, every
noncompleted Mac-job inventory must be complete and idle, and its single private
operation directory must not already exist before any attempted transaction.
The caller reuses the supervisor's existing `~/ci/.gh_token` source for bounded
metadata GETs; the PAT stays in HOST memory/the `gh` child environment and is not
logged, copied to the laptop, or written into a new authentication configuration.
It never sources the supervisor shell script.
Every unfinished job needs a valid identity/status and explicit nonempty string
labels before it can be classified as non-Mac. Unknown inventory blocks the
transaction. Metadata stdout/stderr are limited in flight to4MiB/64KiB; every
read/exit shares the command and complete-inventory deadlines. A failure stops
and reaps only that owned metadata child; partial output is not returned.

The controlled `--failure-canary` injects exit37 after verified old-service
bootout, before replacement. Healthy recovery must preserve the old pair and
GOLDEN, establish a fresh old-source PID/start identity, release only the owned
drain, and retain the failed exit status. That fresh identity needs a subsequent
exact reviewed pin update before a separate `--apply`; PID1872 is not claimed to
survive either restart. The first caller revision explicitly denied `--apply`.
On 2026-10-28, its native controlled canary returned37 through the child and SSH,
reported recovered/admission-restored, and preserved the original source, absent
helper, plist, GOLDEN and all three protected images. The recovered process is
PID99763 with the start above at that point; PID1872 did not survive the restart.
The later formal installation replaced it with PID5158, started
`Wed Oct 28 10:16:07 2026`. Preserved
receipts remain under `supervisor-install-16b72f3a4f7346c99b576a2b07213d6b`.

This post-canary caller revision enables the apply path for a separately reviewed
native invocation and uses a different operation directory,
`supervisor-install-b0eb9e93876a4f5ca5c30a9e93d649b7`. Its default remains plan-only;
the exact successful main/CI, complete idle queue and native guards still apply.
Source authorization alone does not establish installation or permission to
interrupt an active CI VM.

## Native installation acceptance

On 2026-10-28, the reviewed caller installed the exact a31 supervisor/a644 helper
pair after confirming the successful main CI and a complete idle queue. The
unchanged plist bootstrapped a fresh source-bound process, the seven transaction
receipts recorded success, and the owned drain was released. An independent
read-only HOST check confirmed the installed pair, process identity, receipt
hashes, unchanged original/source/new-image storage, six stopped VMs and absent
drain. The original `GOLDEN=macos-ci` remains configured.

The same source passed 86 offline tests on remote AMD and the Mac HOST's native
Python 3.9.6. The earlier real failure canary unloaded the original service,
injected exit37 before replacement, restored a fresh old-source service and
resumed admission while preserving exit37. Neither result establishes a new JIT
job's keychain/helper execution, Xcode 27 image qualification, SIGKILL recovery
or power-loss recovery. The one-shot installation receipts must not be replayed.
