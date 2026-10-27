"""Exercise actual recovery decisions with disposable, deterministic native facts."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance_recovery import MacOperations, RecoveryError, guarded_phase, identity


def storage(tag):
    return {'lima.yaml': {'sha256': tag, 'device': 1, 'inode': tag + '-yaml', 'links': 1},
            'disk': {'device': 1, 'inode': tag + '-disk', 'links': 1},
            'vz-aux': {'device': 1, 'inode': tag + '-aux', 'links': 1}}


class Facts:
    def __init__(self):
        self.events = []
        self.ownership_operation = 'original-owned-maintenance'
        self.marker = True
        self.marker_id = {'inode': 14}
        self.control = {'GOLDEN': 'macos-ci', 'service': {'pid': 44, 'start': 'fixed'}}
        self.facts = {'owned_state': 'Stopped', 'original_state': 'Stopped', 'source_state': 'Stopped',
                      'job_VMs': [], 'other_active_VMs': [], 'owned_storage': storage('new'),
                      'original_storage': storage('original'), 'source_storage': storage('source'),
                      'control': self.control}
        self.proof = {'owned_vm': 'owned-maintenance', 'original_golden': 'macos-ci', 'source_vm': 'source',
                      'original_storage': storage('original'), 'source_storage': storage('source'),
                      'owned_identity': identity(storage('new')), 'control': copy.deepcopy(self.control),
                      'marker_identity': copy.deepcopy(self.marker_id), 'creation_record_sha256': 'created',
                      'phase_source_sha256': 'pinned'}
        self.busy = False
        self.stop_fails = False
        self.stop_changes_state = True
        self.lock_busy = False
        self.fail_record = None
        self.all_audit_writes_fail = False
        self.post_resume_fails = False
        self.marker_removal_fails = False

    @contextmanager
    def lock(self):
        self.events.append('lock')
        if self.lock_busy:
            raise RecoveryError('exclusive_maintenance_lock_busy')
        yield

    def observe(self, _proof): return copy.deepcopy(self.facts)
    def marker_identity(self):
        if not self.marker: raise FileNotFoundError('missing marker')
        return copy.deepcopy(self.marker_id)
    def creation_record_hash(self): return 'created'
    def create_owned_drain(self, proof):
        self.record('drain-creation-intent.json', {})
        self.marker = True
        proof.update(marker_identity=copy.deepcopy(self.marker_id), creation_record_sha256='created')
        self.record('drain-created.json', {})
    def wait_idle(self, _control, timeout): self.events.append(('idle', timeout))
    def record(self, name, value):
        self.events.append(('record', name))
        if self.all_audit_writes_fail or name == self.fail_record:
            raise OSError('synthetic disk-full private detail')
    def native_quiet(self, name):
        self.events.append(('quiet', name))
        return not self.busy
    def normal_stop(self, name, timeout):
        self.events.append(('stop', name, timeout))
        if self.stop_fails:
            raise RecoveryError('normal_owned_VM_stop_failed')
        if self.stop_changes_state:
            self.facts['owned_state'] = 'Stopped'
    def remove_marker(self):
        self.events.append('unlink')
        if not self.marker_removal_fails:
            self.marker = False
    def marker_present(self): return self.marker
    def verify_preserved_after_resume(self, _proof):
        self.events.append('postcheck')
        if self.post_resume_fails:
            raise RecoveryError('post_resume_protected_state_changed')


class MaintenanceRecoveryTests(unittest.TestCase):
    def execute(self, facts=None, change=None, rc=17):
        facts = facts or Facts()
        def phase():
            facts.events.append('phase')
            facts.facts['owned_state'] = 'Running'
            if change:
                change(facts)
            return rc
        return facts, guarded_phase(facts, facts.proof, phase)

    def assert_no_actions(self, facts):
        self.assertNotIn('unlink', facts.events)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'stop' for e in facts.events))

    def test_failure_recovers_but_preserves_original_nonzero(self):
        facts, outcome = self.execute()
        self.assertEqual(outcome['return_code'], 17)
        self.assertEqual(outcome['phase_return_code'], 17)
        self.assertTrue(outcome['recovered'])
        self.assertTrue(outcome['admission_restored'])
        self.assertFalse(facts.marker)
        self.assertEqual([e for e in facts.events if isinstance(e, tuple) and e[0] == 'stop'],
                         [('stop', 'owned-maintenance', 180)])
        self.assertFalse(outcome['job_or_CI_success_claimed'])

    def test_success_keeps_maintenance_context_and_never_runs_fallback(self):
        facts, outcome = self.execute(rc=0)
        self.assertEqual(outcome['return_code'], 0)
        self.assertFalse(outcome['recovery_attempted'])
        self.assertTrue(facts.marker)
        self.assert_no_actions(facts)

    def followup(self):
        facts = Facts(); facts.facts['owned_state'] = 'Running'
        facts.proof.update(expected_initial_owned_state='Running', ownership_operation=facts.ownership_operation)
        return facts

    def test_running_followup_succeeds_under_original_owned_drain(self):
        facts, outcome = self.execute(self.followup(), rc=0)
        self.assertEqual(outcome['return_code'], 0)
        self.assertTrue(outcome['phase_started'])
        self.assertTrue(facts.marker)
        self.assertLess(facts.events.index(('quiet', 'owned-maintenance')), facts.events.index('phase'))
        self.assert_no_actions(facts)

    def test_failed_running_followup_restores_old_admission_and_original_rc(self):
        facts, outcome = self.execute(self.followup(), rc=23)
        self.assertEqual(outcome['return_code'], 23)
        self.assertTrue(outcome['recovered'])
        self.assertFalse(facts.marker)
        self.assertEqual([event for event in facts.events if isinstance(event, tuple) and event[0] == 'stop'],
                         [('stop', 'owned-maintenance', 180)])

    def test_running_followup_busy_or_foreign_ownership_never_enters_phase(self):
        for reason in ('busy', 'foreign-marker', 'foreign-creation'):
            with self.subTest(reason=reason):
                facts = self.followup()
                if reason == 'busy': facts.busy = True
                elif reason == 'foreign-marker': facts.marker_id['inode'] = 99
                else: facts.proof['ownership_operation'] = 'unrelated-operation'
                facts, outcome = self.execute(facts)
                self.assertEqual(outcome['return_code'], 1)
                self.assertFalse(outcome['phase_started'])
                self.assertNotIn('phase', facts.events)
                self.assertFalse(outcome['recovered'])
                self.assert_no_actions(facts)

    def test_running_followup_requires_actual_running_state_and_never_creates_drain(self):
        for create in (False, True):
            with self.subTest(create=create):
                facts = self.followup()
                if create: facts.proof['create_owned_drain'] = True
                else: facts.facts['owned_state'] = 'Stopped'
                _, outcome = self.execute(facts)
                self.assertEqual(outcome['return_code'], 1)
                self.assertFalse(outcome['phase_started'])
                self.assertNotIn('phase', facts.events)
                self.assertNotIn(('record', 'drain-created.json'), facts.events)

    def test_already_stopped_failure_only_restores_admission(self):
        facts, outcome = self.execute(change=lambda f: f.facts.update(owned_state='Stopped'), rc=29)
        self.assertEqual(outcome['return_code'], 29)
        self.assertTrue(outcome['recovered'])
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'stop' for e in facts.events))

    def test_busy_native_work_never_stopped(self):
        facts = Facts(); facts.busy = True
        facts, outcome = self.execute(facts)
        self.assertEqual(outcome['return_code'], 17)
        self.assertFalse(outcome['recovered'])
        self.assert_no_actions(facts)

    def test_changed_marker_never_removed(self):
        facts, outcome = self.execute(change=lambda f: f.marker_id.update(inode=99))
        self.assertFalse(outcome['recovered'])
        self.assert_no_actions(facts)

    def test_control_storage_identity_or_other_job_drift_prevents_actions(self):
        changes = [lambda f: f.control.update(GOLDEN='new-golden'),
                   lambda f: f.facts['original_storage']['disk'].update(inode='changed'),
                   lambda f: f.facts['source_storage']['disk'].update(inode='changed'),
                   lambda f: f.facts['owned_storage']['vz-aux'].update(inode='changed'),
                   lambda f: f.facts.update(job_VMs=['macos-job-healthy']),
                   lambda f: f.facts.update(other_active_VMs=['unrelated'])]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                facts, outcome = self.execute(change=change)
                self.assertEqual(outcome['return_code'], 17)
                self.assertFalse(outcome['recovered'])
                self.assert_no_actions(facts)

    def test_failed_or_unverified_normal_stop_keeps_drain(self):
        for fail, changes in ((True, True), (False, False)):
            with self.subTest(fail=fail):
                facts = Facts(); facts.stop_fails = fail; facts.stop_changes_state = changes
                facts, outcome = self.execute(facts)
                self.assertEqual(outcome['return_code'], 17)
                self.assertFalse(outcome['recovered'])
                self.assertNotIn('unlink', facts.events)

    def test_busy_lock_never_starts_phase(self):
        facts = Facts(); facts.lock_busy = True
        facts, outcome = self.execute(facts)
        self.assertEqual(outcome['return_code'], 1)
        self.assertNotIn('phase', facts.events)
        self.assert_no_actions(facts)

    def test_phase_exception_details_are_private_and_failure_stays_nonzero(self):
        def fail(_facts): raise ValueError('synthetic-secret-sentinel')
        _, outcome = self.execute(change=fail)
        self.assertEqual(outcome['return_code'], 1)
        self.assertTrue(outcome['recovered'])
        self.assertNotIn('synthetic-secret-sentinel', json.dumps(outcome))

    def test_keyboard_interrupt_recovers_but_returns_130(self):
        def interrupt(_facts): raise KeyboardInterrupt()
        _, outcome = self.execute(change=interrupt)
        self.assertEqual(outcome['return_code'], 130)
        self.assertTrue(outcome['recovered'])

    def test_system_exit_preserves_integer_phase_return_code(self):
        def exits(_facts): raise SystemExit(17)
        _, outcome = self.execute(change=exits)
        self.assertEqual(outcome['phase_return_code'], 17)
        self.assertEqual(outcome['return_code'], 17)
        self.assertTrue(outcome['recovered'])

    def test_TERM_and_HUP_recover_with_signal_code_and_restore_handlers(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(number=number):
                previous = signal.getsignal(number)
                def terminate(_facts): os.kill(os.getpid(), number)
                _, outcome = self.execute(change=terminate)
                self.assertEqual(outcome['return_code'], 128 + number)
                self.assertTrue(outcome['recovered'])
                self.assertEqual(signal.getsignal(number), previous)

    def test_stop_audit_ENOSPC_does_not_strand_verified_owned_drain(self):
        facts = Facts(); facts.fail_record = 'normal-stop-intent.json'
        facts, outcome = self.execute(facts)
        self.assertEqual(outcome['return_code'], 17)
        self.assertTrue(outcome['recovered'])
        self.assertIn('normal-stop-intent.json', outcome['evidence_unavailable'])
        self.assertFalse(facts.marker)

    def test_failure_evidence_is_published_before_stop_or_unlink(self):
        facts, _ = self.execute()
        failure = facts.events.index(('record', 'phase-failure.json'))
        self.assertLess(failure, facts.events.index(('stop', 'owned-maintenance', 180)))
        facts = Facts(); facts.fail_record = 'phase-failure.json'
        facts, outcome = self.execute(facts)
        self.assertEqual(outcome['return_code'], 17)
        self.assertTrue(outcome['recovered'])
        self.assertIn('phase-failure.json', outcome['evidence_unavailable'])

    def test_owned_drain_preparation_occurs_inside_lock_before_arming(self):
        facts = Facts()
        def prepare():
            self.assertEqual(facts.events, ['lock'])
            facts.events.append('prepare')
            return facts.proof
        def phase():
            facts.events.append('phase')
            return 0
        outcome = guarded_phase(facts, prepare, phase)
        self.assertEqual(outcome['return_code'], 0)
        self.assertLess(facts.events.index('prepare'), facts.events.index(('record', 'armed.json')))
        self.assertLess(facts.events.index(('record', 'armed.json')), facts.events.index('phase'))

    def test_post_creation_ENOSPC_recovers_before_phase_launch(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                facts = Facts(); facts.marker = False; facts.fail_record = 'drain-created.json'
                facts.proof['create_owned_drain'] = True
                publish = facts.record
                def record(name, value):
                    if name == 'drain-created.json' and persistent: facts.all_audit_writes_fail = True
                    publish(name, value)
                facts.record = record
                outcome = guarded_phase(facts, lambda: facts.proof, lambda: self.fail('phase must not run'))
                self.assertEqual(outcome['return_code'], 1)
                self.assertFalse(outcome['phase_started'])
                self.assertTrue(outcome['recovered'])
                self.assertFalse(facts.marker)
                self.assertFalse(any(isinstance(e, tuple) and e[0] == 'stop' for e in facts.events))

    def test_failed_read_only_prepare_never_adopts_an_existing_marker(self):
        facts = Facts()
        def prepare(): raise RuntimeError('read only proof unavailable')
        outcome = guarded_phase(facts, prepare, lambda: self.fail('phase must not run'))
        self.assertEqual(outcome['return_code'], 1)
        self.assertTrue(facts.marker)
        self.assertFalse(outcome['recovered'])
        self.assert_no_actions(facts)

    def test_preflight_reason_is_limited_to_controlled_recovery_errors(self):
        for error in (RecoveryError('original_runner_service_unknown'), RuntimeError('private native detail')):
            with self.subTest(kind=type(error).__name__):
                facts = Facts()
                def prepare(): raise error
                outcome = guarded_phase(facts, prepare, lambda: self.fail('phase must not run'))
                self.assertEqual(outcome['return_code'], 1)
                self.assertFalse(outcome['phase_started'])
                self.assertEqual(outcome.get('phase_failure_reason'),
                                 'original_runner_service_unknown' if type(error) is RecoveryError else None)
                self.assertNotIn('private native detail', json.dumps(outcome))
                self.assert_no_actions(facts)

    def test_arming_failure_does_not_launch_but_recovers_owned_drain(self):
        facts = Facts(); facts.fail_record = 'armed.json'
        facts, outcome = self.execute(facts)
        self.assertEqual(outcome['return_code'], 1)
        self.assertFalse(outcome['phase_started'])
        self.assertNotIn('phase', facts.events)
        self.assertTrue(outcome['recovered'])

    def test_outcome_write_failure_keeps_actual_recovery_and_original_rc(self):
        facts = Facts(); facts.fail_record = 'phase-outcome.json'
        _, outcome = self.execute(facts)
        self.assertEqual(outcome['return_code'], 17)
        self.assertTrue(outcome['recovered'])
        self.assertTrue(outcome['recovery_evidence_write_failed'])

    def test_persistent_ENOSPC_after_arm_still_safely_recovers_nonzero(self):
        facts, outcome = self.execute(change=lambda f: setattr(f, 'all_audit_writes_fail', True), rc=17)
        self.assertEqual(outcome['return_code'], 17)
        self.assertTrue(outcome['recovered'])
        self.assertFalse(facts.marker)
        self.assertEqual(set(outcome['evidence_unavailable']), {'phase-failure.json', 'normal-stop-intent.json',
                         'drain-removal-intent.json', 'phase-outcome.json'})

    def test_persistent_ENOSPC_never_overrides_foreign_marker_or_unknown_identity(self):
        for foreign in ('marker', 'VM'):
            with self.subTest(foreign=foreign):
                def change(facts):
                    facts.all_audit_writes_fail = True
                    if foreign == 'marker': facts.marker_id['inode'] = 99
                    else: facts.facts['owned_storage']['disk']['inode'] = 'foreign'
                facts, outcome = self.execute(change=change)
                self.assertEqual(outcome['return_code'], 17)
                self.assertFalse(outcome['recovered'])
                self.assert_no_actions(facts)

    def test_successful_command_but_failed_evidence_is_not_green(self):
        facts = Facts(); facts.fail_record = 'phase-outcome.json'
        _, outcome = self.execute(facts, rc=0)
        self.assertEqual(outcome['phase_return_code'], 0)
        self.assertEqual(outcome['return_code'], 1)
        self.assertTrue(outcome['phase_evidence_write_failed'])

    def test_marker_removal_or_postcheck_failure_is_unrecovered(self):
        for marker_failure in (True, False):
            with self.subTest(marker_failure=marker_failure):
                facts = Facts()
                facts.marker_removal_fails = marker_failure
                facts.post_resume_fails = not marker_failure
                _, outcome = self.execute(facts)
                self.assertEqual(outcome['return_code'], 17)
                self.assertFalse(outcome['recovered'])

    def test_unknown_VM_state_and_wrong_original_GOLDEN_fail_closed(self):
        facts, outcome = self.execute(change=lambda f: f.facts.update(owned_state='Stopping'))
        self.assertFalse(outcome['recovered']); self.assert_no_actions(facts)
        facts = Facts(); facts.proof['control']['GOLDEN'] = 'new-golden'
        facts, outcome = self.execute(facts)
        self.assertEqual(outcome['return_code'], 1)
        self.assertNotIn('phase', facts.events)


class NativeAdapterTests(unittest.TestCase):
    def native(self):
        return MacOperations('/unused/operation', '/unused/creation.json', 'pinned', lock_path='/unused/lock')

    def test_launchctl_uses_only_top_level_service_state_and_pid(self):
        # Sanitized shape captured from the actual macOS 26.7.2 service.
        valid = ('gui/501/com.nycu-life.mac-ci-supervisor = {\n'
                 '\tstate = running\n\tpid = 1872\n'
                 '\tresource coalition = {\n\t\tstate = active\n\t\tpid = 99\n\t}\n'
                 '\tjetsam coalition = {\n\t\tstate = active\n\t}\n}\n')
        for text, accepted in ((valid, True), (valid.replace('\tstate = running\n', ''), False),
                               (valid.replace('\tpid = 1872\n', ''), False),
                               (valid.replace('\tstate = running\n', '\tstate = waiting\n'), False),
                               (valid.replace('\tpid = 1872\n', '\tpid = 1872\n\tpid = 1873\n'), False)):
            with self.subTest(text=text):
                native = self.native()
                replies = [subprocess.CompletedProcess([], 0, b'{}\n', b''),
                           subprocess.CompletedProcess([], 0, text.encode(), b''),
                           subprocess.CompletedProcess([], 0, b'fixed process start\n', b'')]
                with mock.patch.object(native, '_run', side_effect=replies) as run:
                    if accepted:
                        self.assertEqual(native._service(), {'pid': 1872, 'start': 'fixed process start', 'GOLDEN': 'macos-ci'})
                        self.assertEqual(run.call_args.args[0], ['/bin/ps', '-p', '1872', '-o', 'lstart='])
                    else:
                        with self.assertRaises(RecoveryError): native._service()

    def test_actual_nested_pause_receipt_path_and_unsafe_ancestors(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            # Actual retained-failure path shape from the immutable PR216 pause.
            name = 'golden-qualification-0dcc2b522e2d4fddbbdb1d215d39049a/unresolved.json'
            path = stage / name; path.parent.mkdir(mode=0o700); path.write_text('{}'); path.chmod(0o600)
            native = self.native(); original = Path.lstat
            def owned501(value):
                result = list(original(value)); result[4] = 501; return os.stat_result(result)
            with mock.patch.object(Path, 'lstat', owned501):
                self.assertEqual(native.private_relative_record(stage, name), path)
                for unsafe in ('../outside.json', '/tmp/outside.json', 'x//y.json', './x.json', 'x/../y.json', 'x\\y.json'):
                    with self.subTest(unsafe=unsafe), self.assertRaises(RecoveryError):
                        native.private_relative_record(stage, unsafe)
                path.parent.chmod(0o755)
                with self.assertRaises(RecoveryError): native.private_relative_record(stage, name)
                path.parent.chmod(0o700)
                (stage / 'linked').symlink_to(path.parent, target_is_directory=True)
                with self.assertRaises(RecoveryError): native.private_relative_record(stage, 'linked/unresolved.json')

    def test_opened_lock_replacement_never_acquires_a_different_flock(self):
        with tempfile.TemporaryDirectory() as folder:
            operation = Path(folder) / 'operation'; operation.mkdir(mode=0o700)
            lock = Path(folder) / 'shared.lock'; lock.write_bytes(b''); lock.chmod(0o600)
            native = MacOperations(operation, operation / 'creation.json', None, lock_path=lock)
            lstat, opened, fstat = Path.lstat, os.open, os.fstat
            def owned(result):
                values = list(result); values[4] = 501; return os.stat_result(values)
            def replace(path, flags, *args, **kwargs):
                lock.rename(lock.with_suffix('.old')); lock.write_bytes(b''); lock.chmod(0o600)
                return opened(path, flags, *args, **kwargs)
            with mock.patch.object(Path, 'lstat', lambda path: owned(lstat(path))), \
                 mock.patch('maintenance_recovery.os.fstat', side_effect=lambda descriptor: owned(fstat(descriptor))), \
                 mock.patch('maintenance_recovery.os.getuid', return_value=501), \
                 mock.patch('maintenance_recovery.os.geteuid', return_value=501), \
                 mock.patch('maintenance_recovery.os.open', side_effect=replace), \
                 mock.patch('maintenance_recovery.fcntl.flock') as acquire:
                with self.assertRaises(RecoveryError):
                    with native.lock(): self.fail('changed lock must never enter')
                acquire.assert_not_called()

    def test_empty_or_malformed_simulator_inventory_is_not_quiet(self):
        for value in ({}, {'devices': {}}, {'devices': {'runtime': 'malformed'}},
                      {'devices': {'runtime': [{'state': 'Booted'}]}}):
            with self.subTest(value=value):
                native = self.native(); calls = []
                def run(args, timeout=20):
                    calls.append(args)
                    return subprocess.CompletedProcess(args, 0, b'' if len(calls) == 1 else json.dumps(value).encode(), b'')
                native._run = run
                self.assertFalse(native.native_quiet('owned-maintenance'))

    def test_normal_stop_never_adds_force_delete_or_other_VM(self):
        native = self.native(); calls = []
        def run(args, timeout=20):
            calls.append((args, timeout))
            return subprocess.CompletedProcess(args, 0, b'', b'')
        native._run = run
        native.normal_stop('owned-maintenance', timeout=180)
        self.assertEqual(calls, [(['/opt/homebrew/bin/limactl', 'stop', 'owned-maintenance'], 180)])

    def test_atomic_private_evidence_never_overwrites_a_prior_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            native = self.native(); native.operation = Path(folder)
            native.record('phase-failure.json', {'phase_return_code': 17})
            path = native.operation / 'phase-failure.json'
            before = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
            with self.assertRaises(FileExistsError):
                native.record('phase-failure.json', {'phase_return_code': 0})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(native.operation.iterdir()), [path])

    def test_actual_creation_captures_identity_after_fsync_or_record_ENOSPC(self):
        for failure in ('marker-fsync', 'created-record'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                home = Path(folder); (home / 'ci').mkdir(mode=0o700)
                operation = home / 'operation'; operation.mkdir(mode=0o700)
                native = MacOperations(operation, operation / 'unused.json', None,
                                       lock_path=home / 'unused-lock', home=home)
                # The adapter requires macOS UID501 in production. These owned
                # Linux fixture files use this test process UID; all native
                # creation, descriptor and immutable-record code still runs.
                native._regular = lambda path, private=False: path.lstat()
                proof = {}; record = native.record; fsync = os.fsync; calls = []
                def publish(name, value):
                    if failure == 'created-record' and name == 'drain-created.json':
                        raise OSError('synthetic ENOSPC')
                    return record(name, value)
                def sync(descriptor):
                    calls.append(descriptor)
                    if failure == 'marker-fsync' and len(calls) == 2: raise OSError('synthetic ENOSPC')
                    return fsync(descriptor)
                native.record = publish
                with mock.patch('maintenance_recovery.os.fsync', side_effect=sync):
                    with self.assertRaises(OSError): native.create_owned_drain(proof)
                self.assertEqual(proof['marker_identity'], native.marker_identity())
                self.assertEqual(proof['creation_record_sha256'], native.creation_record_hash())
                self.assertEqual(native.marker.stat().st_mode & 0o777, 0o600)
                self.assertEqual(native.marker.stat().st_nlink, 1)

    def test_followup_namespace_uses_only_pinned_original_creation_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder); (home / 'ci').mkdir(mode=0o700)
            first = home / 'first'; first.mkdir(mode=0o700)
            initial = MacOperations(first, first / 'unused.json', None, lock_path=home / 'lock', home=home)
            initial._regular = lambda path, private=False: path.lstat()
            proof = {}; initial.create_owned_drain(proof)
            followup = home / 'followup'; followup.mkdir(mode=0o700)
            native = MacOperations(followup, initial.creation_record, initial.creation_sha256,
                    lock_path=home / 'lock', home=home, marker_content=initial.marker_content,
                    ownership_operation=first.name)
            native._regular = lambda path, private=False: path.lstat()
            self.assertEqual(native.creation_record_hash(), initial.creation_sha256)
            native.record('armed.json', {'original_operation': first.name})
            self.assertTrue((followup / 'armed.json').is_file())
            self.assertTrue((first / 'drain-created.json').is_file())
            native.ownership_operation = 'unrelated'
            with self.assertRaises(RecoveryError): native.creation_record_hash()


if __name__ == '__main__':
    unittest.main()
