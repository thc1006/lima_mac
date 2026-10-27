# Recover original admission after a failed maintenance phase

`maintenance_recovery.py` is an opt-in adapter for **pre-cutover** image
maintenance. It does not change or install the supervisor, LaunchAgent, runner
labels or GOLDEN. The original live GOLDEN must still identify the original
stopped image. Use it from the reviewed maintenance entrypoint, before starting
the owned maintenance VM.

The entrypoint supplies private, reviewed proofs for the original and source
storage, the owned image identity, and the unchanged running supervisor. Its
read-only proof callback runs under the existing exclusive maintenance flock.
For a new phase, set `create_owned_drain=True`: the engine first publishes an
immutable private creation intent, then uses exclusive creation for its drain.
An existing operator's drain is never adopted. The caller may retain the existing
drain content for compatibility; the creation record and exact file identity
establish ownership.

The phase callback runs under that same lock and must use bounded operations.
It must not reacquire the lock, cut over GOLDEN, cancel jobs, modify the original
or source image, or start unrelated native work. A successful phase retains its
drain for the next reviewed maintenance operation. Success is not qualification
or CI acceptance. Do not invoke an unguarded follow-up phase: use the same
reviewed recovery boundary for its full mutation lifecycle, or pause and restore
admission explicitly before ending maintenance.

For a follow-up while the owned VM is already running, explicitly set
`expected_initial_owned_state='Running'` and `ownership_operation` to the original
creation operation. Use a new private evidence directory and a `MacOperations`
instance pointing to the exact original creation record/hash, original marker
content, and matching `ownership_operation`. The original record is not rewritten
or adopted from a foreign operator. This opt-in path verifies the exact running
VM identity, immutable creation proof, marker and native quiescence before the
callback; it never creates a drain or infers that the GUI is unlocked. Success
keeps the same owned drain. Failure uses the same normal stop/admission recovery.

If the phase fails, its immutable failure record is attempted before recovery.
Once the durable owned-drain creation proof exists, later audit-write failures
cannot strand that verified drain: recovery continues under the full identity
guards and lists unavailable evidence by filename in its returned/stdout outcome.
This includes persistent ENOSPC after arming. The original nonzero code remains;
the operator must retain the stdout/local receipt when the host cannot write.
Recovery checks the live controls, protected storage, exact drain ownership and
all other VM states. It normally stops only the owned VM, after verifying no
native build, runner, updater or booted simulator is present. It then verifies
all protected images stopped, the original manager freshly drained and idle,
and the exact marker again before unlinking it. The normal existing supervisor
may immediately admit a healthy job; recovery never cancels or stops that job.
Busy, missing or ambiguous facts leave recovery unresolved and admission paused.

**A recovered failure still exits nonzero.** Return `outcome['return_code']`, not
the boolean `recovered`. An integer `SystemExit` retains its code, keyboard
interruption returns 130, and SIGTERM/SIGHUP become 128 plus the signal number.
Normal signal handlers run only during this synchronous main-thread boundary
and are restored afterwards. Post-create evidence/fsync failures retain the
exact marker identity for safe cleanup without starting the phase.

SIGKILL, host power loss, or an interpreter failure cannot execute Python
cleanup. The operation's immutable intents remain for explicit reviewed
reconciliation at the next startup; this patch does not install an automatic
crash replayer. Never delete those records or rerun the same operation to hide
an unresolved result. If exclusive ownership or safe recovery cannot be proven,
report the failure and keep the drain.

The current source-only integration is
`resume-new-golden-maintenance-after-pr216.py` in the operator workspace. It pins
this source, creates a new owned drain after the active CI job and VM cleanup
finish, and wraps the actual normal NEW-image start plus fresh OS/boot metadata
phase. It leaves live supervisor configuration unchanged. Subsequent GUI or
qualification phases require their own reviewed, fully wrapped boundary.
Deployment and native fault-controlled acceptance remain separate from the
Linux fixtures; no live installation is implied by this patch.

Run the deterministic fault checks on a disposable Linux validation host:

```sh
python3 nycu-life/tests/test_maintenance_recovery.py -v
python3 nycu-life/tests/test_supervisor_drain.py -v
python3 nycu-life/tests/test_unlock_keychain.py -v
```

The fixtures exercise failed phases, original exit codes, TERM/HUP, post-create
and persistent ENOSPC, immutable evidence, replaced locks, safe nested receipt
paths, lock contention, changed storage/control/marker,
native work, failed normal stop and unresolved postchecks. They are not native
macOS, GUI, golden qualification, JIT or full CI acceptance.

The supervisor source now preserves runner, log-writer and cleanup failures
([#5](https://github.com/thc1006/lima_mac/pull/5)). Merging source does not
update a running HOST supervisor; verify its deployed revision before relying
on that behavior. Separate existing findings remain: a failed `df` query uses
the permissive default, and startup reconciliation force-deletes clones by
name prefix. These need independent scoped fixes and acceptance.
