"""HOST cutover fixtures only: no VM, launchd, credentials or network access."""
from contextlib import contextmanager, redirect_stdout
import copy
import io
import json
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import golden_plist_cutover as cutover
import os
if os.environ.get('CUTOVER_UNDER_TEST'):
    import importlib.util
    _spec = importlib.util.spec_from_file_location('cutover_baseline', os.environ['CUTOVER_UNDER_TEST'])
    cutover = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(cutover)
CANARY_HOOK = cutover.FailureCanaryOperations.after_new_plist_installed


def snapshot(seed=1):
    value = {name: {'device': 1, 'inode': seed + index, 'links': 1,
                    'size': 100, 'mtime_ns': 123456789}
             for index, name in enumerate(('lima.yaml', 'disk', 'vz-aux'))}
    value['lima.yaml']['sha256'] = 'a' * 64
    return value


def qualified():
    storage = snapshot()
    operation = cutover.QUALIFICATION_OPERATION
    records = {
        'image-preflight-passed-stopped.json': {'image': cutover.NEW_GOLDEN,
            'storage': storage, 'clean_clone_smoke_passed': True,
            'owned_probe_deleted': True, 'full_CI_or_JIT_accepted': False},
        'main-toolchain-compatibility.json': {'head': cutover.MAIN_SHA,
            'source_sha256': cutover.MAIN_VERIFIER_SHA, 'return_code': 0,
            'cutover_blocker': False, 'full_CI_accepted': False,
            'commands_executed_in': 'macos-xcode27-smoke-' + operation.rsplit('-', 1)[1]},
        'phase-outcome.json': {'return_code': 0, 'phase_return_code': 0,
                               'admission_restored': False},
    }
    for name, expected in (('verify-toolchain-checked-result.json', cutover.NEW_VERIFIER_SHA),
                           ('verify-toolchain-main-checked-result.json', cutover.MAIN_VERIFIER_SHA)):
        filename = name.removesuffix('-checked-result.json').replace('-', '_') + '-checked-report.private.raw'
        records[name] = {'source_sha256': expected, 'return_code': 0,
                        'full_Native27_accepted': False, 'report': {
                            'private_path': '/Users/thc1006/ci/xcode27-guest-prep-20261027/' + operation + '/' + filename,
                            'bytes': len(b'actual-fixture-verifier-report\n'),
                            'sha256': cutover.digest(b'actual-fixture-verifier-report\n')}}
    raw = {name: json.dumps(value).encode() for name, value in records.items()}
    pair = {name: {'device': 1, 'inode': index + 10, 'owner': 501,
                   'mode': 0o700, 'links': 1, 'size': 100, 'mtime_ns': 12345,
                   'sha256': expected} for index, (name, expected) in enumerate(cutover.PAIR.items())}
    native = {'status': cutover.QUALIFICATION_VERDICT,
        'current_control': {'plist_sha256': cutover.OLD_PLIST_SHA,
            'supervisor_sha256': cutover.PAIR['mac-ci-supervisor.sh'],
            'GOLDEN': cutover.OLD_GOLDEN, 'service': dict(cutover.INITIAL_SERVICE)},
        'current_pair': pair, 'current_VM_states': {name: 'Stopped' for name in cutover.VMS},
        'new_stopped_storage': storage, 'original_phase_code': 0,
        'original_and_source_storage_exact': True, 'probe_inventory_and_path_absent': True,
        'drain_absent': True, 'admission_restored': True, 'clean_clone_passed': True,
        'operation_receipts': {name: {'sha256': cutover.digest(value)} for name, value in raw.items()}}
    accepted = {'qualification_schema_version': 1, 'qualification_operation': operation,
        'verdict': cutover.QUALIFICATION_VERDICT, 'new_stopped_storage': storage,
        'native_readonly_result': native}
    return accepted, raw


def change_record(accepted, raw, name, key, value):
    record = json.loads(raw[name]); record[key] = value
    raw[name] = json.dumps(record).encode()
    accepted['native_readonly_result']['operation_receipts'][name]['sha256'] = cutover.digest(raw[name])


class Facts:
    """Fault-injected native observations; use the real rollback/release methods."""
    restore_old = cutover.PlistCutoverOperations.restore_old
    release = cutover.PlistCutoverOperations.release

    def __init__(self, failure=None, *, secondary=None, canary=False):
        self.failure = failure; self.secondary = secondary; self.events = []; self.records = {}
        self.marker = None; self.kind = 'old'; self.locked = False; self.counter = 6000
        self.native_uncertain = False; self.file_uncertain = False
        self.release_started = False; self.released_kind = None; self.bootstrap_intended = False
        self.unavailable_records = []; self.canary = canary; self.canary_injected = False
        self.hashes = {'old': cutover.OLD_PLIST_SHA, 'new': 'b' * 64}
        self.plist_file = {'sha256': self.hashes['old']}
        self.service = dict(cutover.INITIAL_SERVICE); self.foreign = False; self.full = False

    def event(self, name):
        assert self.locked
        self.events.append(name)
        if self.failure == name:
            self.failure = None
            raise cutover.RecoveryError(name)

    @contextmanager
    def lock(self):
        if self.failure == 'lock': raise cutover.RecoveryError('lock_busy')
        self.locked = True
        try: yield
        finally: self.locked = False

    def control(self):
        if self.service is None: raise cutover.RecoveryError('no_service')
        return {'plist_sha256': self.hashes[self.kind],
            'supervisor_sha256': cutover.PAIR['mac-ci-supervisor.sh'],
            'GOLDEN': self.service['GOLDEN'], 'service': dict(self.service)}

    def _authority(self, kind):
        if self.foreign or self.file_uncertain or self.kind != kind:
            raise cutover.RecoveryError('foreign_file_or_pair')

    def _control(self, kind):
        self._authority(kind)
        value = self.control()
        if value['GOLDEN'] != (cutover.OLD_GOLDEN if kind == 'old' else cutover.NEW_GOLDEN):
            raise cutover.RecoveryError('wrong_effective_GOLDEN')
        return value

    def prepare(self):
        self.event('prepare')
        if self.marker is not None: raise cutover.RecoveryError('foreign_drain')
        return {'control': self.control(), 'started_epoch': time.time() - 1,
                'VM_states': {name: 'Stopped' for name in cutover.VMS}}

    def record(self, name, value):
        self.event('record:' + name)
        if self.full: raise OSError('fixture_ENOSPC')
        if name in self.records: raise FileExistsError(name)
        self.records[name] = copy.deepcopy(value)

    def _optional_record(self, name, value):
        try: self.record(name, value)
        except Exception: self.unavailable_records.append(name)

    def create_owned_drain(self, proof):
        self.event('create-drain'); self.marker = 'owned'; proof['marker_identity'] = 'owned'
        self.event('drain-created')

    def marker_present(self): return self.marker is not None
    def marker_identity(self): return self.marker

    def verify_boundary(self, proof):
        self.event('boundary')
        if self.marker != proof['marker_identity'] or self.foreign:
            raise cutover.RecoveryError('owned_boundary_changed')

    def verify(self, proof, kind, control):
        self.verify_boundary(proof); self.event('verify:' + kind)
        if self._control(kind) != control: raise cutover.RecoveryError('service_changed')

    def wait_idle(self, control, timeout):
        self.event('idle:' + ('old' if control['GOLDEN'] == cutover.OLD_GOLDEN else 'new'))

    def bootout(self, control):
        kind = 'old' if control['GOLDEN'] == cutover.OLD_GOLDEN else 'new'
        self.event('bootout:' + kind)
        assert self.marker == 'owned' and self.control() == control
        self.service = None; self.event('bootout-stopped:' + kind)

    def _absent(self, control):
        self.event('absent')
        if self.service is not None: raise cutover.RecoveryError('service_absence_unproven')

    def install_plist(self, kind):
        self.event('install:' + kind)
        assert self.marker == 'owned' and self.service is None
        if kind == 'new' and self.secondary == 'unknown-replace':
            self.file_uncertain = True; raise cutover.RecoveryError('replace_outcome_unknown')
        self.kind = kind; self.plist_file = {'sha256': self.hashes[kind]}
        if kind == 'new' and self.secondary == 'ENOSPC': self.full = True
        self.event('installed:' + kind)

    def after_new_plist_installed(self):
        self.event('after-new-plist')
        if self.canary: CANARY_HOOK(self)
        if self.secondary == 'unknown-native':
            self.native_uncertain = True; raise cutover.RecoveryError('launchd_outcome_unknown')
        if self.secondary == 'foreign':
            self.foreign = True; raise cutover.RecoveryError('foreign_identity')
        if self.secondary == 'foreign-drain':
            self.marker = 'foreign'; raise cutover.RecoveryError('foreign_drain')
        if self.secondary == 'TERM': raise cutover.MaintenanceTermination(signal.SIGTERM)
        if self.secondary == 'HUP': raise cutover.MaintenanceTermination(signal.SIGHUP)
        if self.secondary == 'INT': raise KeyboardInterrupt()

    def bootstrap(self, kind, proof):
        self.event('bootstrap:' + kind)
        assert self.marker == 'owned' and self.service is None
        self.bootstrap_intended = True; self.counter += 1
        self.service = {'pid': self.counter, 'start': time.strftime('%a %b %d %H:%M:%S %Y'),
                        'GOLDEN': cutover.OLD_GOLDEN if kind == 'old' else cutover.NEW_GOLDEN}
        self.event('bootstrapped:' + kind)
        return self._control(kind)

    def cleanup_stages(self): self.event('cleanup-stages')

    def remove_marker(self):
        self.event('unlink-before'); self.marker = None; self.event('unlink-after')


class CutoverTests(unittest.TestCase):
    def test_implicit_original_GOLDEN_can_be_activated_without_forging_old_key(self):
        before = {'EnvironmentVariables': {'HOME': 'fixture'}, 'KeepAlive': True}
        make = getattr(cutover, 'bridge_plist', None) or cutover.one_key_plist
        try:
            after = plistlib.loads(make(plistlib.dumps(before)))
        except cutover.RecoveryError:
            after = None
        self.assertIsNotNone(after, 'Actual a31 default macos-ci must be accepted without inventing old GOLDEN')
        self.assertNotIn('GOLDEN', before['EnvironmentVariables'])
        self.assertEqual(after['EnvironmentVariables']['GOLDEN'], cutover.NEW_GOLDEN)

    def test_two_typed_keys_preserve_every_other_value_and_original_absence(self):
        for env in ({'OTHER': 'keep'}, {'GOLDEN': cutover.OLD_GOLDEN, 'OTHER': 'keep'},
                    {'GOLDEN': cutover.OLD_GOLDEN, 'ADDITIONAL_LABEL': '', 'LABEL': 'nycu-life-macos'}):
            before = {'Label': 'fixture', 'EnvironmentVariables': env,
                      'KeepAlive': True, 'Nested': [False, 0, True, 1, b'bytes']}
            raw = plistlib.dumps(before, sort_keys=False)
            after = plistlib.loads(cutover.bridge_plist(raw))
            self.assertEqual(after['EnvironmentVariables']['GOLDEN'], cutover.NEW_GOLDEN)
            self.assertEqual(after['EnvironmentVariables']['ADDITIONAL_LABEL'], cutover.ADDITIONAL_LABEL)
            for key in ('GOLDEN', 'ADDITIONAL_LABEL'):
                if key in env:
                    after['EnvironmentVariables'][key] = env[key]
                else:
                    del after['EnvironmentVariables'][key]
            self.assertEqual(cutover.typed(before), cutover.typed(after))
            self.assertEqual(plistlib.loads(raw), before)
        self.assertNotEqual(cutover.typed(True), cutover.typed(1))

    def test_wrong_typed_or_already_active_original_is_refused(self):
        for value in ({}, {'EnvironmentVariables': []},
                      {'EnvironmentVariables': {'GOLDEN': 'other'}},
                      {'EnvironmentVariables': {'GOLDEN': True}},
                      {'EnvironmentVariables': {'LABEL': 'other'}},
                      {'EnvironmentVariables': {'ADDITIONAL_LABEL': cutover.ADDITIONAL_LABEL}},
                      {'EnvironmentVariables': {'ADDITIONAL_LABEL': False}}):
            with self.subTest(value=value), self.assertRaises(cutover.RecoveryError):
                cutover.bridge_plist(plistlib.dumps(value))

    def test_each_actual_verifier_receipt_is_required_and_strict(self):
        for name in ('verify-toolchain-checked-result.json', 'verify-toolchain-main-checked-result.json'):
            for key, value in [('return_code', 1), ('return_code', True),
                               ('source_sha256', '0' * 64), ('full_Native27_accepted', True),
                               ('report', {'bytes': 0}), ('report', {'bytes': 2, 'sha256': 'a' * 64,
                                   'private_path': '/some/old/operation/report'})]:
                accepted, raw = qualified()
                change_record(accepted, raw, name, key, value)
                with self.subTest(name=name, key=key, value=value), self.assertRaises(cutover.RecoveryError):
                    cutover.qualification_facts(accepted, raw)
            accepted, raw = qualified(); del raw[name]
            with self.assertRaises(cutover.RecoveryError):
                cutover.qualification_facts(accepted, raw)

    def test_rollback_stages_original_plist_bytes_not_a_reserialization(self):
        original = plistlib.dumps({'EnvironmentVariables': {'HOME': 'fixture'},
                                   'KeepAlive': True}, fmt=plistlib.FMT_BINARY, sort_keys=False)
        class StageReached(Exception): pass
        ops = object.__new__(cutover.PlistCutoverOperations)
        ops.home = Path('/Users/thc1006')
        ops.live_plist = ops.home / 'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist'
        ops.pair = {'fixture': 'kept'}; ops.files = ops.pair
        ops.seen_processes = set(); ops.protected_storage = {}; ops.operation = ops.home / 'fixture'
        ops.staged_plist = {}; backups = []; staged = []
        metadata = {'sha256': cutover.digest(original), 'mode': 0o600}
        ops.marker_present = lambda: False; ops._pair = lambda: ops.pair
        ops._file = lambda p: metadata
        ops._read_expected = lambda p, m: original
        ops._control = lambda kind: {'service': cutover.INITIAL_SERVICE}
        ops._quiet = lambda: {}; ops._queue = lambda: {}
        ops._backup = lambda name, raw: backups.append((name, raw))
        def stage(name, raw, mode, directory=None):
            staged.append((name, raw)); return (Path('/fixture/' + name), {})
        ops._stage = stage
        ops._sync = lambda _: None
        with mock.patch.object(cutover, 'OLD_PLIST_SHA', cutover.digest(original)), \
             mock.patch.object(cutover.sys, 'platform', 'darwin'), \
             mock.patch.object(cutover.os, 'getuid', return_value=501), \
             mock.patch.object(cutover.os, 'geteuid', return_value=501), \
             mock.patch.object(Path, 'lstat', return_value=type('M', (), {'st_mode': stat.S_IFDIR | 0o700, 'st_uid': 501})()), \
             mock.patch.object(Path, 'resolve', lambda p: p), \
             mock.patch.object(Path, 'is_symlink', return_value=False), \
             mock.patch.object(cutover.os, 'statvfs', return_value=type('V', (), {'f_bavail': 40 * 1024**3, 'f_frsize': 1})()):
            ops.prepare()
        self.assertEqual(backups, [('original.plist', original)])
        self.assertEqual(dict(staged)['old.plist'], original)
        self.assertNotEqual(dict(staged)['new.plist'], original)
        self.assertNotIn('GOLDEN', plistlib.loads(dict(staged)['old.plist'])['EnvironmentVariables'])

    def test_default_and_disabled_apply_are_exact_file_cli_with_no_native_actions(self):
        script = str(Path(cutover.__file__).resolve())
        for flags in ([], ['--apply'], ['--failure-canary']):
            with self.subTest(flags=flags):
                value = subprocess.run([sys.executable, '-I', '-B', script, *flags],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=5, env={})
                result = json.loads(value.stdout)
                self.assertEqual(value.stderr, b'')
                if flags:
                    self.assertEqual(value.returncode, 1)
                    self.assertEqual(result['failure_reason'], 'final_qualification_pin_and_source_review_required_before_apply')
                else:
                    self.assertEqual(value.returncode, 0)
                    self.assertEqual(result['mode'], 'plan_only')
                    self.assertIs(result['native_actions'], False)
                    self.assertIs(result['apply_authorized'], False)
                    self.assertIsNone(result['qualification_acceptance_sha256'])

    def test_direct_execute_is_disabled_before_platform_file_or_native_work(self):
        with mock.patch.object(cutover, 'read_owned') as read, mock.patch.object(cutover, 'PlistCutoverOperations') as ops:
            with self.assertRaisesRegex(cutover.RecoveryError, 'final_qualification_pin'):
                cutover.execute()
            read.assert_not_called(); ops.assert_not_called()

    def test_disabled_apply_emits_structured_refusal_without_a_traceback(self):
        value = subprocess.run([sys.executable, '-I', '-B', str(Path(cutover.__file__).resolve()), '--apply'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5, env={})
        self.assertEqual(value.stderr, b'')
        self.assertEqual(value.returncode, 1)
        self.assertEqual(json.loads(value.stdout)['failure_reason'],
                         'final_qualification_pin_and_source_review_required_before_apply')

    def test_main_routes_only_explicit_modes_to_the_real_caller(self):
        for arguments, mode in ((['--apply'], 'apply'), (['--failure-canary'], 'failure-canary')):
            with self.subTest(mode=mode), mock.patch.object(cutover, 'execute', return_value={'return_code': 37}) as execute, redirect_stdout(io.StringIO()):
                self.assertEqual(cutover.main(arguments), 37)
                execute.assert_called_once_with(mode)

    def test_real_execute_reads_only_pinned_records_then_uses_the_selected_transaction_adapter(self):
        directory = Path(cutover.__file__).resolve().parent
        public = {directory / name: (directory / name).read_bytes() for name in
                  ('maintenance_recovery.py', 'supervisor_install.py', 'supervisor_install_entry.py')}
        home = Path('/Users/thc1006'); stage = home / 'ci/xcode27-guest-prep-20261027'
        for mode in ('apply', 'failure-canary'):
            accepted, records = qualified(); accept_raw = json.dumps(accepted).encode()
            created = json.dumps({'original_golden_storage_proof': {'golden': snapshot(100)},
                                  'source_stopped_storage': snapshot(200)}).encode()
            paused = json.dumps({'retained_failure_sha256': {}}).encode()
            files = {**public, directory / 'clean-clone-acceptance.json': accept_raw,
                     stage / 'golden-created-stopped.json': created,
                     stage / 'pr216-new-golden-paused-stopped.json': paused,
                     **{stage / accepted['qualification_operation'] / name: raw for name, raw in records.items()},
                     **{stage / accepted['qualification_operation'] / name: b'actual-fixture-verifier-report\n'
                        for name in ('verify_toolchain-checked-report.private.raw',
                                     'verify_toolchain_main-checked-report.private.raw')}}
            seen = []

            def reader(path, *, private=False, limit=None):
                seen.append(path)
                self.assertIn(path, files)
                self.assertEqual(private, path not in public)
                return files[path]

            facts = Facts(canary=mode == 'failure-canary')
            metadata = type('Directory', (), {'st_uid': 501, 'st_mode': stat.S_IFDIR | 0o700})()
            with self.subTest(mode=mode), mock.patch.object(cutover, 'CUTOVER_ENABLED', True), \
                    mock.patch.object(cutover, 'QUALIFICATION_ACCEPTANCE_SHA', cutover.digest(accept_raw)), \
                    mock.patch.object(cutover, 'CREATED_SHA', cutover.digest(created)), \
                    mock.patch.object(cutover, 'PAUSED_SHA', cutover.digest(paused)), \
                    mock.patch.object(cutover.sys, 'platform', 'darwin'), \
                    mock.patch.object(cutover.os, 'getuid', return_value=501), \
                    mock.patch.object(cutover.os, 'geteuid', return_value=501), \
                    mock.patch.object(Path, 'home', return_value=home), \
                    mock.patch.object(Path, 'resolve', lambda self: self), \
                    mock.patch.object(Path, 'lstat', return_value=metadata), \
                    mock.patch.object(Path, 'is_symlink', return_value=False), \
                    mock.patch.object(Path, 'mkdir') as mkdir, \
                    mock.patch.object(cutover, 'read_owned', side_effect=reader), \
                    mock.patch.object(cutover, 'PlistCutoverOperations', return_value=facts) as apply, \
                    mock.patch.object(cutover, 'FailureCanaryOperations', return_value=facts) as canary:
                facts.retained = {}
                outcome = cutover.execute(mode)
                selected, other = (canary, apply) if mode == 'failure-canary' else (apply, canary)
                selected.assert_called_once(); other.assert_not_called()
                self.assertEqual(selected.call_args.args[0], stage / (cutover.CANARY_OPERATION if mode == 'failure-canary' else cutover.OPERATION))
                self.assertEqual(selected.call_args.kwargs['protected_storage'],
                                 {cutover.OLD_GOLDEN: snapshot(100), cutover.SOURCE: snapshot(200), cutover.NEW_GOLDEN: snapshot()})
                self.assertEqual(selected.call_args.kwargs['accepted_pair'], accepted['native_readonly_result']['current_pair'])
                mkdir.assert_called_once_with(mode=0o700)
            self.assertEqual(set(seen), set(files))
            self.assertEqual(outcome['caller_mode'], mode)
            self.assertEqual(outcome['return_code'], 37 if mode == 'failure-canary' else 0)
            self.assertEqual(outcome['canary_injected'], mode == 'failure-canary')
            self.assertIs(outcome['full_CI_or_JIT_accepted'], False)

    def test_future_success_receipt_requires_matching_records_without_sharing_mutable_state(self):
        accepted, raw = qualified(); facts = cutover.qualification_facts(accepted, raw)
        self.assertEqual(facts['NEW_storage'], snapshot())
        self.assertEqual(set(facts['record_hashes']), set(cutover.QUALIFICATION_RECORDS))
        facts['pair']['mac-ci-supervisor.sh']['inode'] = 900
        self.assertEqual(accepted['native_readonly_result']['current_pair']['mac-ci-supervisor.sh']['inode'], 10)

    def test_successful_phase_receipt_is_pre_release_and_commit_is_observed_separately(self):
        # Capture the reviewed engine's real serialization order, rather than
        # inventing a post-release value for its earlier durable phase receipt.
        from maintenance_recovery import guarded_phase
        from test_maintenance_recovery import Facts as PhaseFacts
        phase = PhaseFacts(); captured = {}; original = phase.record

        def record(name, value):
            original(name, value); captured[name] = copy.deepcopy(value)

        phase.record = record
        engine_outcome = guarded_phase(phase, phase.proof, lambda: 0)
        self.assertEqual(engine_outcome['return_code'], 0)
        self.assertTrue(phase.marker)
        self.assertIs(engine_outcome['admission_restored'], False)
        accepted, raw = qualified()
        raw['phase-outcome.json'] = json.dumps(captured['phase-outcome.json']).encode()
        accepted['native_readonly_result']['operation_receipts']['phase-outcome.json']['sha256'] = cutover.digest(raw['phase-outcome.json'])
        try:
            facts = cutover.qualification_facts(accepted, raw)
        except cutover.RecoveryError:
            facts = None
        self.assertIsNotNone(facts, 'successful guarded_phase receipt precedes admission release')
        self.assertIs(json.loads(raw['phase-outcome.json'])['admission_restored'], False)
        self.assertIs(accepted['native_readonly_result']['admission_restored'], True)
        change_record(accepted, raw, 'phase-outcome.json', 'admission_restored', True)
        with self.assertRaisesRegex(cutover.RecoveryError, 'pre_release'):
            cutover.qualification_facts(accepted, raw)

    def test_old_preflight_failure_or_recovery_receipt_cannot_qualify(self):
        for verdict in ('SCOPED_IMAGE_PREFLIGHT_SUCCESS_ACCEPTANCE_PASS', 'SCOPED_NONZERO_CLEAN_CLONE_RECOVERY_PASS', ''):
            accepted, raw = qualified(); accepted['verdict'] = verdict
            with self.subTest(verdict=verdict), self.assertRaises(cutover.RecoveryError):
                cutover.qualification_facts(accepted, raw)

    def test_untyped_boolean_codes_cannot_stand_in_for_integer_zero_or_version(self):
        for target in ('version', 'phase-code', 'compat-code', 'outcome-code', 'outcome-phase'):
            accepted, raw = qualified()
            if target == 'version': accepted['qualification_schema_version'] = True
            elif target == 'phase-code': accepted['native_readonly_result']['original_phase_code'] = False
            else:
                name = 'main-toolchain-compatibility.json' if target == 'compat-code' else 'phase-outcome.json'
                key = 'phase_return_code' if target == 'outcome-phase' else 'return_code'
                change_record(accepted, raw, name, key, False)
            with self.subTest(target=target), self.assertRaises(cutover.RecoveryError):
                cutover.qualification_facts(accepted, raw)

    def test_missing_positive_native_facts_or_extra_VM_refuse_qualification(self):
        for key in ('original_and_source_storage_exact', 'probe_inventory_and_path_absent', 'drain_absent',
                    'admission_restored', 'clean_clone_passed', 'current_VM_states'):
            accepted, raw = qualified()
            accepted['native_readonly_result'][key] = False if key != 'current_VM_states' else {'unexpected': 'Stopped'}
            with self.subTest(key=key), self.assertRaises(cutover.RecoveryError):
                cutover.qualification_facts(accepted, raw)

    def test_changed_pair_initial_pid_plist_or_new_snapshot_are_refused(self):
        for change in ('pair', 'pid', 'plist', 'storage', 'typed-storage'):
            accepted, raw = qualified(); native = accepted['native_readonly_result']
            if change == 'pair': native['current_pair']['unlock-keychain.py']['sha256'] = '0' * 64
            elif change == 'pid': native['current_control']['service']['pid'] += 1
            elif change == 'plist': native['current_control']['plist_sha256'] = '0' * 64
            elif change == 'storage': native['new_stopped_storage'] = snapshot(900)
            else: accepted['new_stopped_storage']['disk']['links'] = True
            with self.subTest(change=change), self.assertRaises(cutover.RecoveryError):
                cutover.qualification_facts(accepted, raw)

    def test_qualification_operation_cannot_escape_or_adopt_a_preflight(self):
        for name in ('../other', '/tmp/other', 'new-golden-image-preflight-' + 'a' * 32,
                     'new-golden-clean-clone-' + 'a' * 32, 'new-golden-resume-clean-clone-' + 'a' * 32,
                     'new-golden-clean-clone-short'):
            accepted, raw = qualified(); accepted['qualification_operation'] = name
            with self.subTest(name=name), self.assertRaises(cutover.RecoveryError):
                cutover.qualification_facts(accepted, raw)

    def test_seal_hash_payload_and_main_compatibility_are_hard_requirements(self):
        for failure in ('hash', 'seal', 'snapshot', 'compat', 'blocker', 'main-head', 'verifier', 'probe', 'outcome', 'missing'):
            accepted, raw = qualified()
            if failure == 'hash': raw['image-preflight-passed-stopped.json'] += b' '
            elif failure == 'missing': del raw['phase-outcome.json']
            elif failure in ('seal', 'snapshot'):
                change_record(accepted, raw, 'image-preflight-passed-stopped.json',
                              'owned_probe_deleted' if failure == 'seal' else 'storage', False if failure == 'seal' else snapshot(700))
            elif failure == 'outcome': change_record(accepted, raw, 'phase-outcome.json', 'return_code', 1)
            else:
                key, value = {'compat': ('return_code', 1), 'blocker': ('cutover_blocker', True),
                              'main-head': ('head', 'b' * 40), 'verifier': ('source_sha256', 'b' * 64),
                              'probe': ('commands_executed_in', 'foreign-probe')}[failure]
                change_record(accepted, raw, 'main-toolchain-compatibility.json', key, value)
            with self.subTest(failure=failure), self.assertRaises(cutover.RecoveryError):
                cutover.qualification_facts(accepted, raw)

    def test_success_changes_only_plist_and_releases_after_validation(self):
        facts = Facts(); outcome = cutover.cutover_transaction(facts)
        self.assertEqual(outcome['return_code'], 0, outcome)
        self.assertTrue(outcome['switched']); self.assertTrue(outcome['admission_restored'])
        self.assertEqual(facts.kind, 'new'); self.assertIsNone(facts.marker)
        for first, last in (('bootout-stopped:old', 'install:new'), ('installed:new', 'bootstrap:new'),
                            ('idle:new', 'unlink-before'), ('cleanup-stages', 'unlink-before')):
            self.assertLess(facts.events.index(first), facts.events.index(last))
        self.assertNotIn('install:old', facts.events)

    def test_controlled_failure_after_new_plist_before_bootstrap_restores_old_with_fresh_pid(self):
        facts = Facts(canary=True); outcome = cutover.cutover_transaction(facts)
        self.assertEqual(outcome['return_code'], 37, outcome)
        self.assertTrue(facts.canary_injected); self.assertTrue(outcome['recovered'])
        self.assertTrue(outcome['admission_restored']); self.assertFalse(outcome['switched'])
        self.assertNotIn('bootstrap:new', facts.events)
        self.assertEqual(facts.kind, 'old'); self.assertIsNone(facts.marker)
        self.assertNotEqual(facts.service['pid'], cutover.INITIAL_SERVICE['pid'])
        self.assertLess(facts.events.index('installed:new'), facts.events.index('install:old'))
        self.assertEqual(facts.records['cutover-outcome.json']['return_code'], 37)

    def test_failures_at_known_pre_release_boundaries_restore_original_admission(self):
        for failure in ('drain-created', 'bootout:old', 'bootout-stopped:old', 'installed:new',
                        'bootstrap:new', 'bootstrapped:new', 'idle:new', 'record:cutover-drained.json'):
            facts = Facts(failure); outcome = cutover.cutover_transaction(facts)
            with self.subTest(failure=failure):
                self.assertEqual(outcome['return_code'], 1, outcome)
                self.assertTrue(outcome['recovered'], outcome)
                self.assertTrue(outcome['admission_restored'], outcome)
                self.assertEqual(facts.kind, 'old'); self.assertIsNone(facts.marker)
                if failure not in ('drain-created', 'bootout:old'):
                    self.assertNotEqual(facts.service['pid'], cutover.INITIAL_SERVICE['pid'])

    def test_failure_before_mutation_never_boots_out_original(self):
        for failure in ('lock', 'prepare', 'record:cutover-intent.json'):
            facts = Facts(failure); outcome = cutover.cutover_transaction(facts)
            with self.subTest(failure=failure):
                self.assertEqual(outcome['return_code'], 1)
                self.assertNotIn('bootout:old', facts.events)
                self.assertEqual(facts.service, cutover.INITIAL_SERVICE)

    def test_unknown_mutations_or_foreign_identity_retain_drain_without_restart(self):
        for secondary in ('unknown-replace', 'unknown-native', 'foreign', 'foreign-drain'):
            facts = Facts(secondary=secondary); outcome = cutover.cutover_transaction(facts)
            with self.subTest(secondary=secondary):
                self.assertNotEqual(outcome['return_code'], 0)
                self.assertFalse(outcome['recovered'])
                self.assertFalse(outcome['admission_restored'])
                self.assertIsNotNone(facts.marker)
                self.assertNotIn('install:old', facts.events)
                self.assertNotIn('bootstrap:old', facts.events)

    def test_signals_keep_original_nonzero_codes_and_recover_known_boundary(self):
        for secondary, code in (('TERM', 143), ('HUP', 129), ('INT', 130)):
            facts = Facts(secondary=secondary); outcome = cutover.cutover_transaction(facts)
            with self.subTest(secondary=secondary):
                self.assertEqual(outcome['return_code'], code)
                self.assertTrue(outcome['recovered']); self.assertTrue(outcome['admission_restored'])

    def test_optional_rollback_audit_failure_cannot_strand_old_runner(self):
        facts = Facts(secondary='ENOSPC'); outcome = cutover.cutover_transaction(facts)
        self.assertEqual(outcome['return_code'], 1, outcome)
        self.assertTrue(outcome['recovered']); self.assertTrue(outcome['admission_restored'])
        self.assertEqual(facts.kind, 'old'); self.assertIsNone(facts.marker)
        self.assertIn('old-drain-release-intent.json', outcome['evidence_unavailable'])

    def test_no_restart_or_plist_replace_after_release_begins_even_if_unlink_failed(self):
        for failure, marker, switched in (('unlink-before', 'owned', False), ('unlink-after', None, True)):
            facts = Facts(failure); outcome = cutover.cutover_transaction(facts)
            with self.subTest(failure=failure):
                self.assertEqual(outcome['return_code'], 1)
                self.assertTrue(outcome['post_release_failure'])
                self.assertEqual(facts.marker, marker); self.assertEqual(outcome['switched'], switched)
                self.assertEqual(outcome['admission_restored'], marker is None)
                self.assertNotIn('install:old', facts.events)
                self.assertNotIn('bootout:new', facts.events)
                self.assertNotIn('bootstrap:old', facts.events)

    def test_final_receipt_failure_keeps_admitted_new_service_but_reports_nonzero(self):
        facts = Facts('record:cutover-outcome.json'); outcome = cutover.cutover_transaction(facts)
        self.assertEqual(outcome['return_code'], 1)
        self.assertTrue(outcome['switched']); self.assertTrue(outcome['admission_restored'])
        self.assertTrue(outcome['post_commit_evidence_failure'])
        self.assertNotIn('bootout:new', facts.events); self.assertNotIn('install:old', facts.events)

    def test_native_replace_uncertainty_is_sticky_but_successful_replace_is_tracked_before_fsync(self):
        for failure in ('replace', 'sync'):
            original = {'sha256': 'a'}; replacement = {'sha256': 'b'}
            native = type('Native', (), {'native_uncertain': False, 'file_uncertain': False,
                'active_proof': {}, 'live_plist': Path('/fixture/live'), 'plist_file': original,
                'staged_plist': {'new': (Path('/fixture/staged'), replacement)},
                'verify_boundary': lambda self, proof: None,
                '_file': lambda self, path: replacement if path.name == 'staged' else self.plist_file,
                '_authority': lambda self, kind: None, '_sync': mock.Mock(side_effect=OSError('sync') if failure == 'sync' else None)})()
            with self.subTest(failure=failure), mock.patch.object(cutover.os, 'replace', side_effect=OSError('replace') if failure == 'replace' else None), self.assertRaises(OSError):
                cutover.PlistCutoverOperations.install_plist(native, 'new')
            self.assertEqual(native.file_uncertain, failure == 'replace')
            self.assertEqual(native.plist_file, original if failure == 'replace' else replacement)


if __name__ == '__main__':
    unittest.main()
