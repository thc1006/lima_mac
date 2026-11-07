"""Disabled HOST caller: typed GOLDEN/capability activation with source-bound rollback.

No script/helper replacement, VM action, disable flag, credentials or new pool.
Apply stays disabled until the final clean-clone acceptance is pinned and reviewed.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from maintenance_recovery import (MacOperations, MaintenanceTermination, RecoveryError,
                                  digest, require, termination_boundary)
from supervisor_install import InstallOperations
from supervisor_install_entry import QueueProof, read_owned

ENGINE_SHA = '922c306ac5f8d2a157bdb7502861c60d14aaf8d25ee18edff6b1a027539bee7c'
INSTALLER_SHA = 'b7d808df4a5a2dd62595eb713a037adc6a809101bb159dc4ae3774156c0720ab'
CALLER_SHA = '71a2f067513d6a3042209ac302aadab9558306b1c15295faca2d4b739a81fa2d'
OLD_PLIST_SHA = '5cf9d1e8be1dc0907919006938d4271d6177e85e6dd89f28fda996046b085e96'
PAIR = {'mac-ci-supervisor.sh': '9ce061cf7f3294764e9fec80cde9615ce412adf323ff55f3af31b189def1c828',
        'unlock-keychain.py': 'a644e6ed903ef3b4b68d9737b259629b0925b4ce5b8e871c7737f749c6d08142'}
OLD_GOLDEN = 'macos-ci'
NEW_GOLDEN = 'macos-ci-xcode27-20261027'
SOURCE = 'macos-xcode27-candidate-20261027'
VMS = {OLD_GOLDEN, NEW_GOLDEN, SOURCE, 'apns-review-20261025', 'k3s', 'k3s-agent1'}
INITIAL_SERVICE = {'pid': 5158, 'start': 'Wed Oct 28 10:16:07 2026', 'GOLDEN': OLD_GOLDEN}
OPERATION = 'golden-plist-cutover-77067c057dea47c0889b2c48f46a9985'
CANARY_OPERATION = 'golden-plist-cutover-862054db3bd34f8b8a1314ff85cdd5f8'
CUTOVER_ENABLED = False
QUALIFICATION_ACCEPTANCE_SHA = None
QUALIFICATION_VERDICT = 'SCOPED_CLEAN_CLONE_SUCCESS_ACCEPTANCE_PASS'
# Planned resume operation only; this name is not evidence of qualification.
QUALIFICATION_OPERATION = 'new-golden-clean-clone-f9fc53b3e2e84a8b9574d9e07678de92'
CREATED_SHA = 'dd7fd7384bbb9f0aeb7be2b2f7db4cf033d6750f8638d08db0009ed07c58f308'
PAUSED_SHA = '3f242d02bad4d8d5ca6b8a3aa1bd93a861260ad3c9c206df3edfe2c4b2681e14'
MAIN_SHA = '7a798a01bcb6ccb46d8cbb3cdbc0ea97252eac41'
MAIN_VERIFIER_SHA = '756ed2bcd87ecaa893c0d9214f684a217b6b36363a4aa85a90ba08d01c78b49f'
NEW_VERIFIER_SHA = '4786f16c1f22c8e312cc5e52b7331395f493403a5e85611b5fb1370f443d5a6c'
ADDITIONAL_LABEL = 'nycu-life-macos-xcode-27'
QUALIFICATION_RECORDS = ('image-preflight-passed-stopped.json',
                         'main-toolchain-compatibility.json', 'phase-outcome.json',
                         'verify-toolchain-checked-result.json',
                         'verify-toolchain-main-checked-result.json')


def typed(value):
    """Preserve bool/int and container types while comparing parsed plists."""
    if type(value) is dict:
        return ('dict', tuple(sorted((key, typed(item)) for key, item in value.items())))
    if type(value) is list:
        return ('list', tuple(typed(item) for item in value))
    return (type(value).__name__, value)


def bridge_plist(raw):
    """Change only two allowlisted keys; preserve implicit old defaults on rollback."""
    before = plistlib.loads(raw)
    require(type(before) is dict and type(before.get('EnvironmentVariables')) is dict,
            'original_environment_dictionary_required')
    env = before['EnvironmentVariables']
    require(type(env.get('GOLDEN', OLD_GOLDEN)) is str
            and env.get('GOLDEN', OLD_GOLDEN) == OLD_GOLDEN,
            'original_effective_GOLDEN_required')
    require(type(env.get('LABEL', 'nycu-life-macos')) is str
            and env.get('LABEL', 'nycu-life-macos') == 'nycu-life-macos'
            and type(env.get('ADDITIONAL_LABEL', '')) is str
            and env.get('ADDITIONAL_LABEL', '') == '', 'original_generic_label_required')
    after = copy.deepcopy(before)
    after['EnvironmentVariables'].update(GOLDEN=NEW_GOLDEN, ADDITIONAL_LABEL=ADDITIONAL_LABEL)
    encoded = plistlib.dumps(after, fmt=plistlib.FMT_XML, sort_keys=False)
    decoded = plistlib.loads(encoded)
    require(decoded['EnvironmentVariables']['GOLDEN'] == NEW_GOLDEN
            and decoded['EnvironmentVariables']['ADDITIONAL_LABEL'] == ADDITIONAL_LABEL,
            'new_bridge_serialization_changed')
    for key in ('GOLDEN', 'ADDITIONAL_LABEL'):
        if key in env:
            decoded['EnvironmentVariables'][key] = env[key]
        else:
            del decoded['EnvironmentVariables'][key]
    require(typed(decoded) == typed(before), 'only_typed_bridge_keys_permitted')
    return encoded


def error_code(error):
    return (128 + error.number if isinstance(error, MaintenanceTermination)
            else 130 if isinstance(error, KeyboardInterrupt)
            else error.code if isinstance(error, SystemExit) and type(error.code) is int
            and 1 <= error.code <= 255 else 1)


def cutover_transaction(ops):
    """A failed known transaction restores old plist/admission; never report green."""
    result = dict(return_code=1, switched=False, recovered=False,
                  admission_restored=False, evidence_unavailable=[])
    proof = None
    def audit(name, value):
        try:
            ops.record(name, value)
            return True
        except Exception:
            result['evidence_unavailable'].append(name)
            return False
    try:
        with termination_boundary(), ops.lock():
            try:
                proof = ops.prepare()
                ops.record('cutover-intent.json', proof)
                ops.create_owned_drain(proof)
                ops.verify(proof, 'old', proof['control'])
                ops.wait_idle(proof['control'], timeout=35)
                ops.verify(proof, 'old', proof['control'])
                ops.record('old-bootout-intent.json', {'control': proof['control']})
                ops.bootout(proof['control'])
                ops.install_plist('new')
                ops.after_new_plist_installed()
                ops.record('new-bootstrap-intent.json', {'plist_sha256': ops.hashes['new'], 'GOLDEN': NEW_GOLDEN})
                control = ops.bootstrap('new', proof)
                ops.wait_idle(control, timeout=35)
                ops.verify(proof, 'new', control)
                ops.record('cutover-drained.json', {'control': control, 'pair_unchanged': PAIR})
                ops.cleanup_stages()
                ops.release(proof, 'new', control)
                result.update(return_code=0, switched=True, admission_restored=True,
                              service=control['service'])
            except BaseException as error:
                code = error_code(error)
                result.update(return_code=code, failure_type=type(error).__name__)
                if type(error) is RecoveryError:
                    result['failure_reason'] = str(error)
                audit('cutover-failure.json', result)
                if ops.release_started:
                    # Unlink may already have admitted work. No later error may
                    # bootout, replace or restart either process or plist.
                    result.update(post_release_failure=True,
                                  admission_restored=not ops.marker_present(),
                                  switched=ops.released_kind == 'new' and not ops.marker_present())
                else:
                    try:
                        if proof is not None and 'marker_identity' in proof:
                            ops.verify_boundary(proof)
                            control = ops.restore_old(proof)
                            ops.wait_idle(control, timeout=35)
                            ops.verify(proof, 'old', control)
                            ops.cleanup_stages()
                            audit('rollback-healthy.json', {'control': control, 'original_return_code': code})
                            ops.release(proof, 'old', control)
                            result.update(recovered=True, admission_restored=True,
                                          rollback_service=control['service'])
                        elif ops.marker_present():
                            raise RecoveryError('unknown_or_foreign_drain_retained')
                        else:
                            ops.cleanup_stages()
                    except BaseException as recovery_error:
                        result.update(recovery_failure_type=type(recovery_error).__name__,
                                      recovery_reason=str(recovery_error) if type(recovery_error) is RecoveryError
                                      else 'private_recovery_failure')
            if not audit('cutover-outcome.json', result) and result['return_code'] == 0:
                result.update(return_code=1, post_commit_evidence_failure=True)
    except BaseException as error:
        result.update(return_code=error_code(error), preflight_failure_type=type(error).__name__)
    result['evidence_unavailable'].extend(getattr(ops, 'unavailable_records', []))
    return result


class PlistCutoverOperations(InstallOperations):
    """Reuse reviewed native/file primitives, with an independent plist authority."""
    def __init__(self, operation, *, protected_storage, accepted_pair, retained, queue_probe, home=None):
        home = Path.home() if home is None else Path(home)
        stage = home / 'ci/xcode27-guest-prep-20261027'
        MacOperations.__init__(self, operation, Path(operation) / 'drain-creation-intent.json',
                               None, lock_path=stage / 'toolchain-install.lock', home=home)
        require(home == Path('/Users/thc1006') and self.operation.parent == stage
                and re.fullmatch(r'golden-plist-cutover-[0-9a-f]{32}', self.operation.name),
                'exact_plist_cutover_operation_required')
        self.live_plist = home / 'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist'
        self.protected_storage = copy.deepcopy(protected_storage)
        self.pair = copy.deepcopy(accepted_pair)
        self.retained = dict(retained)
        self.queue_probe = queue_probe
        self.plist_file = None
        self.hashes = {}
        self.staged_plist = {}
        self.active_proof = None
        self.native_uncertain = False
        self.file_uncertain = False
        self.bootstrap_intended = False
        self.seen_processes = set()
        self.unavailable_records = []
        self.release_started = False
        self.released_kind = None

    def _authority(self, kind):
        require(kind in ('old', 'new') and not self.file_uncertain, 'plist_replace_outcome_unknown')
        require(self._pair() == self.pair and set(self.pair) == set(PAIR)
                and all(self.pair[name] is not None and self.pair[name]['sha256'] == expected
                        and self.pair[name]['mode'] == 0o700 for name, expected in PAIR.items()),
                'unchanged_script_helper_authority_required')
        require(self._file(self.live_plist) == self.plist_file
                and self.plist_file['sha256'] == self.hashes[kind], 'expected_plist_authority_changed')

    def _quiet(self, proof=None):
        require(not self.file_uncertain and self._file(self.live_plist) == self.plist_file,
                'tracked_plist_identity_changed')
        require(self._pair() == self.pair, 'script_or_helper_identity_changed')
        result = self._run([self.lima, 'list', '--json'])
        require(result.returncode == 0, 'VM_inventory_unavailable')
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        states = {row['name']: row['status'] for row in rows}
        require(len(states) == len(rows) and set(states) == VMS
                and all(state == 'Stopped' for state in states.values()), 'exact_six_stopped_VMs_required')
        require(set(self.protected_storage) == {OLD_GOLDEN, NEW_GOLDEN, SOURCE}
                and all(self.storage(name) == value for name, value in self.protected_storage.items()),
                'full_protected_storage_changed')
        require(all(digest(read_owned(path, private=True)) == expected for path, expected in self.retained.items()),
                'pinned_qualification_or_historical_evidence_changed')
        if proof is not None:
            require(states == proof['VM_states'], 'VM_inventory_changed')
        return states

    def control(self):
        current = self._file(self.live_plist)
        require(not self.file_uncertain and current == self.plist_file, 'live_plist_identity_changed')
        configured = plistlib.loads(self._read_expected(self.live_plist, current))['EnvironmentVariables'].get('GOLDEN', OLD_GOLDEN)
        service = self._service()
        require(configured == service['GOLDEN'], 'effective_GOLDEN_changed')
        require(self._pair() == self.pair, 'script_or_helper_identity_changed')
        return {'plist_sha256': current['sha256'], 'supervisor_sha256': PAIR['mac-ci-supervisor.sh'],
                'GOLDEN': configured, 'service': service}

    def _control(self, kind):
        self._authority(kind)
        control = self.control()
        require(control['GOLDEN'] == (OLD_GOLDEN if kind == 'old' else NEW_GOLDEN)
                and control['plist_sha256'] == self.hashes[kind], 'plist_service_authority_changed')
        pid = control['service']['pid']
        value = self._run(['/bin/ps', '-p', str(pid), '-o', 'uid=,pgid=,comm='])
        fields = value.stdout.decode().split()
        require(value.returncode == 0 and len(fields) == 3 and fields[:2] == ['501', str(pid)]
                and fields[2] in ('bash', '/bin/bash'), 'service_native_process_identity_changed')
        return control

    def prepare(self):
        require(sys.platform == 'darwin' and os.getuid() == os.geteuid() == 501, 'Mac_HOST501_required')
        require(not self.marker_present() and self._pair() == self.pair, 'idle_original_pair_required')
        for directory in (self.home / 'ci', self.live_plist.parent):
            value = directory.lstat()
            require(stat.S_ISDIR(value.st_mode) and directory.resolve() == directory and not directory.is_symlink()
                    and value.st_uid == 501 and not value.st_mode & 0o022, 'protected_live_parent_required')
        self.plist_file = self._file(self.live_plist)
        require(self.plist_file is not None and self.plist_file['sha256'] == OLD_PLIST_SHA
                and self.plist_file['mode'] in (0o600, 0o644), 'exact_original_plist_required')
        original = self._read_expected(self.live_plist, self.plist_file)
        replacement = bridge_plist(original)
        self.hashes = {'old': digest(original), 'new': digest(replacement)}
        control = self._control('old')
        require(control['service'] == INITIAL_SERVICE, 'initial_5158_start_identity_changed_requires_review')
        self.seen_processes.add(control['service']['pid'])
        reserve = os.statvfs(self.home)
        require(reserve.f_bavail * reserve.f_frsize >= 20 * 1024**3, 'minimum_20GiB_required')
        proof = {'control': control, 'VM_states': self._quiet(), 'storage': self.protected_storage,
                 'queue': self._queue(), 'pair': self.pair, 'plist_hashes': dict(self.hashes),
                 'only_typed_change': ['EnvironmentVariables.GOLDEN', 'EnvironmentVariables.ADDITIONAL_LABEL'], 'started_epoch': time.time()}
        self._backup('original.plist', original)
        self.staged_plist['old'] = self._stage('old.plist', original, self.plist_file['mode'], directory=self.operation)
        self.staged_plist['new'] = self._stage('new.plist', replacement, self.plist_file['mode'], directory=self.live_plist.parent)
        self._sync(self.operation)
        self._sync(self.live_plist.parent)
        self.active_proof = proof
        return proof

    def install_plist(self, kind):
        require(not self.native_uncertain and not self.file_uncertain, 'unresolved_mutation_no_plist_replace')
        self.verify_boundary(self.active_proof)
        path, expected = self.staged_plist[kind]
        require(self._file(path) == expected and self._file(self.live_plist) == self.plist_file,
                'staged_or_live_plist_identity_changed')
        self.file_uncertain = True
        os.replace(path, self.live_plist)
        self.plist_file = expected
        self.file_uncertain = False
        self._sync(self.live_plist.parent)
        self._authority(kind)

    def after_new_plist_installed(self):
        """Only the separate controlled canary overrides this pre-bootstrap point."""

    def bootstrap(self, kind, proof):
        self.verify_boundary(proof)
        self._authority(kind)
        self.bootstrap_intended = True
        value = self._run(['/bin/launchctl', 'bootstrap', 'gui/501', str(self.live_plist)])
        deadline = time.monotonic() + 15
        while True:
            try:
                control = self._control(kind)
                require(control['service']['pid'] not in self.seen_processes, 'replacement_PID_not_fresh')
                started = time.mktime(time.strptime(control['service']['start'], '%a %b %d %H:%M:%S %Y'))
                require(proof['started_epoch'] - 1 <= started <= time.time() + 1, 'replacement_start_not_fresh')
                self.seen_processes.add(control['service']['pid'])
                record = {'control': control, 'plist_authority': kind, 'pair_unchanged': PAIR}
                if kind == 'old': self._optional_record('old-service-observed.json', record)
                else: self.record('new-service-observed.json', record)
                require(value.returncode == 0, 'native_bootstrap_failed')
                return control
            except RecoveryError:
                if time.monotonic() >= deadline: raise
                time.sleep(.25)

    def restore_old(self, proof):
        require(not self.native_uncertain and not self.file_uncertain and not self.release_started,
                'unknown_or_released_boundary_no_rollback')
        self.verify_boundary(proof)
        try:
            current = self.control()
        except RecoveryError:
            current = None
        if current == proof['control'] and self.plist_file['sha256'] == self.hashes['old']:
            return self._control('old')
        if current is not None:
            require(self.bootstrap_intended, 'unknown_service_not_owned')
            kind = 'new' if self.plist_file['sha256'] == self.hashes['new'] else 'old'
            current = self._control(kind)
            started = time.mktime(time.strptime(current['service']['start'], '%a %b %d %H:%M:%S %Y'))
            require(current['service']['pid'] != proof['control']['service']['pid']
                    and proof['started_epoch'] - 1 <= started <= time.time() + 1,
                    'rollback_process_identity_not_owned')
            self._optional_record('replacement-bootout-intent.json', {'control': current})
            self.bootout(current)
        else:
            self._absent(proof['control'])
        if self.plist_file['sha256'] != self.hashes['old']:
            self.install_plist('old')
        self._optional_record('old-bootstrap-intent.json', {'plist_sha256': self.hashes['old']})
        return self.bootstrap('old', proof)

    def cleanup_stages(self):
        staged = self.staged_plist.get('new')
        if staged is not None:
            path, expected = staged
            if os.path.lexists(path):
                require(self._file(path) == expected, 'staged_plist_cleanup_identity_changed')
                path.unlink()
                self._sync(path.parent)

    def release(self, proof, kind, control):
        self.verify(proof, kind, control)
        value = {'control': control, 'plist_authority': kind, 'marker_identity': proof['marker_identity']}
        if kind == 'old': self._optional_record('old-drain-release-intent.json', value)
        else: self.record('new-drain-release-intent.json', value)
        self.verify(proof, kind, control)
        require(self.marker_identity() == proof['marker_identity'], 'marker_changed_before_release')
        # Conservative boundary: an exception during unlink must not trigger a
        # restart after admission may already have resumed.
        self.release_started = True
        self.released_kind = kind
        self.remove_marker()
        require(not self.marker_present(), 'owned_marker_release_unverified')


class FailureCanaryOperations(PlistCutoverOperations):
    def after_new_plist_installed(self):
        self.canary_injected = True
        raise SystemExit(37)


def storage_proof(value):
    require(type(value) is dict and set(value) == {'lima.yaml', 'disk', 'vz-aux'},
            'complete_storage_snapshot_required')
    fields = {'device', 'inode', 'links', 'size', 'mtime_ns'}
    for name, item in value.items():
        require(type(item) is dict and set(item) == fields | ({'sha256'} if name == 'lima.yaml' else set())
                and all(type(item[key]) is int and item[key] >= 0 for key in fields)
                and item['device'] > 0 and item['inode'] > 0 and item['links'] == 1,
                'typed_full_storage_snapshot_required')
        if name == 'lima.yaml':
            require(type(item['sha256']) is str and re.fullmatch(r'[0-9a-f]{64}', item['sha256']),
                    'storage_config_hash_required')
    return copy.deepcopy(value)


def qualification_facts(accepted, records):
    """Check a future independently reviewed success receipt, never an old preflight.

    The caller reads the three fixed records from the named private operation.
    Their hashes must match the pinned acceptance, and their contents must agree.
    This is metadata qualification only, not JIT or full native CI acceptance.
    """
    require(type(accepted) is dict and accepted.get('verdict') == QUALIFICATION_VERDICT
            and accepted.get('qualification_schema_version') == 1
            and type(accepted.get('qualification_schema_version')) is int,
            'final_clean_clone_acceptance_schema_required')
    operation = accepted.get('qualification_operation')
    require(type(operation) is str and operation == QUALIFICATION_OPERATION,
            'exact_qualification_operation_required')
    native = accepted.get('native_readonly_result')
    require(type(native) is dict and native.get('status') == QUALIFICATION_VERDICT
            and all(native.get(name) is True for name in ('original_and_source_storage_exact',
                    'probe_inventory_and_path_absent', 'drain_absent', 'admission_restored', 'clean_clone_passed'))
            and type(native.get('original_phase_code')) is int and native['original_phase_code'] == 0,
            'successful_clean_clone_native_observation_required')
    require(native.get('current_VM_states') == {name: 'Stopped' for name in VMS},
            'exact_six_stopped_VM_receipt_required')
    control = native.get('current_control')
    require(type(control) is dict and control == {'plist_sha256': OLD_PLIST_SHA,
            'supervisor_sha256': PAIR['mac-ci-supervisor.sh'], 'GOLDEN': OLD_GOLDEN,
            'service': INITIAL_SERVICE}, 'qualified_original_control_required')
    pair = native.get('current_pair')
    metadata = {'device', 'inode', 'owner', 'mode', 'links', 'size', 'mtime_ns', 'sha256'}
    require(type(pair) is dict and set(pair) == set(PAIR), 'qualified_unchanged_pair_required')
    for name, expected in PAIR.items():
        item = pair[name]
        require(type(item) is dict and set(item) == metadata
                and all(type(item[key]) is int and item[key] >= 0 for key in metadata - {'sha256'})
                and item['device'] > 0 and item['inode'] > 0 and item['owner'] == 501
                and item['mode'] == 0o700 and item['links'] == 1 and item['sha256'] == expected,
                'qualified_pair_metadata_required')
    storage = storage_proof(accepted.get('new_stopped_storage'))
    require(typed(native.get('new_stopped_storage')) == typed(storage), 'qualified_NEW_snapshot_disagrees')
    hashes = native.get('operation_receipts')
    require(type(hashes) is dict and type(records) is dict and set(records) == set(QUALIFICATION_RECORDS),
            'fixed_qualification_records_required')
    parsed, retained = {}, {}
    for name in QUALIFICATION_RECORDS:
        item = hashes.get(name)
        require(type(item) is dict and type(item.get('sha256')) is str
                and re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                and type(records[name]) is bytes and digest(records[name]) == item['sha256'],
                'qualification_record_hash_mismatch')
        parsed[name] = json.loads(records[name])
        require(type(parsed[name]) is dict, 'qualification_record_object_required')
        retained[name] = item['sha256']
    seal = parsed['image-preflight-passed-stopped.json']
    require(seal.get('image') == NEW_GOLDEN and seal.get('clean_clone_smoke_passed') is True
            and seal.get('owned_probe_deleted') is True and seal.get('full_CI_or_JIT_accepted') is False
            and typed(seal.get('storage')) == typed(storage), 'clean_clone_seal_required')
    compatibility = parsed['main-toolchain-compatibility.json']
    require(compatibility.get('head') == MAIN_SHA and compatibility.get('source_sha256') == MAIN_VERIFIER_SHA
            and type(compatibility.get('return_code')) is int and compatibility['return_code'] == 0
            and compatibility.get('cutover_blocker') is False and compatibility.get('full_CI_accepted') is False
            and compatibility.get('commands_executed_in') == 'macos-xcode27-smoke-' + operation.rsplit('-', 1)[1],
            'unchanged_main_toolchain_compatibility_required')
    report_records = {}
    for name, expected in (('verify-toolchain-checked-result.json', NEW_VERIFIER_SHA),
                           ('verify-toolchain-main-checked-result.json', MAIN_VERIFIER_SHA)):
        checked = parsed[name]
        report = checked.get('report')
        filename = name.removesuffix('-checked-result.json').replace('-', '_') + '-checked-report.private.raw'
        expected_path = str(Path('/Users/thc1006/ci/xcode27-guest-prep-20261027') / operation / filename)
        require(checked.get('source_sha256') == expected
                and type(checked.get('return_code')) is int and checked['return_code'] == 0
                and checked.get('full_Native27_accepted') is False
                and type(report) is dict and report.get('private_path') == expected_path
                and type(report.get('bytes')) is int and 0 < report['bytes'] <= 1024 * 1024
                and type(report.get('sha256')) is str and re.fullmatch(r'[0-9a-f]{64}', report['sha256']),
                'both_actual_current_toolchain_verifier_receipts_required')
        report_records[filename] = {'sha256': report['sha256'], 'bytes': report['bytes']}
    outcome = parsed['phase-outcome.json']
    # guarded_phase durably records success while admission is still drained.
    # finish_success releases later; the pinned independent native observation
    # above and prepare()'s fresh live guards prove admission was then restored.
    require(type(outcome.get('return_code')) is int and outcome['return_code'] == 0
            and type(outcome.get('phase_return_code')) is int and outcome['phase_return_code'] == 0
            and outcome.get('admission_restored') is False, 'durable_pre_release_qualification_outcome_required')
    return {'operation': operation, 'pair': copy.deepcopy(pair), 'NEW_storage': storage,
            'record_hashes': retained, 'report_records': report_records}


def enabled():
    require(CUTOVER_ENABLED is True and type(QUALIFICATION_ACCEPTANCE_SHA) is str
            and re.fullmatch(r'[0-9a-f]{64}', QUALIFICATION_ACCEPTANCE_SHA),
            'final_qualification_pin_and_source_review_required_before_apply')


def execute(mode='apply'):
    enabled()  # Even a direct caller cannot bypass the disabled draft boundary.
    require(mode in ('apply', 'failure-canary'), 'only_reviewed_cutover_mode_allowed')
    require(sys.platform == 'darwin' and os.getuid() == os.geteuid() == 501, 'Mac_HOST501_required')
    directory = Path(__file__).resolve().parent
    for name, expected in (('maintenance_recovery.py', ENGINE_SHA), ('supervisor_install.py', INSTALLER_SHA),
                           ('supervisor_install_entry.py', CALLER_SHA)):
        require(digest(read_owned(directory / name)) == expected, 'reviewed_cutover_dependency_changed')
    acceptance_path = directory / 'clean-clone-acceptance.json'
    raw = read_owned(acceptance_path, private=True)
    require(digest(raw) == QUALIFICATION_ACCEPTANCE_SHA, 'exact_final_qualification_acceptance_required')
    accepted = json.loads(raw)
    home = Path.home(); stage = home / 'ci/xcode27-guest-prep-20261027'
    value = stage.lstat()
    require(stage.resolve() == stage and not stage.is_symlink() and stat.S_ISDIR(value.st_mode)
            and value.st_uid == 501 and stat.S_IMODE(value.st_mode) == 0o700, 'private_management_stage_required')
    name = accepted.get('qualification_operation') if type(accepted) is dict else None
    require(type(name) is str and name == QUALIFICATION_OPERATION,
            'exact_qualification_operation_required')
    qualifying_operation = stage / name
    value = qualifying_operation.lstat()
    require(qualifying_operation.resolve() == qualifying_operation and not qualifying_operation.is_symlink()
            and stat.S_ISDIR(value.st_mode) and value.st_uid == 501 and stat.S_IMODE(value.st_mode) == 0o700,
            'private_qualification_operation_required')
    records = {name: read_owned(qualifying_operation / name, private=True) for name in QUALIFICATION_RECORDS}
    facts = qualification_facts(accepted, records)
    for filename, report in facts['report_records'].items():
        body = read_owned(qualifying_operation / filename, private=True, limit=1024 * 1024)
        require(len(body) == report['bytes'] and digest(body) == report['sha256'],
                'actual_verifier_report_bytes_mismatch')
    retained = {acceptance_path: QUALIFICATION_ACCEPTANCE_SHA,
                **{qualifying_operation / name: expected for name, expected in facts['record_hashes'].items()},
                **{qualifying_operation / name: value['sha256'] for name, value in facts['report_records'].items()}}
    created_path = stage / 'golden-created-stopped.json'
    created_raw = read_owned(created_path, private=True)
    require(digest(created_raw) == CREATED_SHA, 'original_storage_receipt_changed')
    created = json.loads(created_raw)
    protected = {OLD_GOLDEN: storage_proof(created['original_golden_storage_proof']['golden']),
                 SOURCE: storage_proof(created['source_stopped_storage']), NEW_GOLDEN: facts['NEW_storage']}
    paused_path = stage / 'pr216-new-golden-paused-stopped.json'
    paused_raw = read_owned(paused_path, private=True)
    require(digest(paused_raw) == PAUSED_SHA, 'historical_pause_receipt_changed')
    retained.update({created_path: CREATED_SHA, paused_path: PAUSED_SHA})
    operation = stage / (CANARY_OPERATION if mode == 'failure-canary' else OPERATION)
    adapter = FailureCanaryOperations if mode == 'failure-canary' else PlistCutoverOperations
    ops = adapter(operation, protected_storage=protected, accepted_pair=facts['pair'],
                  retained=retained, queue_probe=QueueProof())
    history = json.loads(paused_raw)['retained_failure_sha256']
    require(type(history) is dict, 'historical_failure_hashes_required')
    for relative, expected in history.items():
        require(type(expected) is str and re.fullmatch(r'[0-9a-f]{64}', expected), 'historical_failure_hash_required')
        path = ops.private_relative_record(stage, relative)
        require(digest(read_owned(path, private=True)) == expected, 'historical_failure_changed')
        ops.retained[path] = expected
    operation.mkdir(mode=0o700)  # O_EXCL-like directory creation forbids replay/adoption.
    outcome = cutover_transaction(ops)
    outcome.update(caller_mode=mode, operation=str(operation),
                   qualification_acceptance_sha256=QUALIFICATION_ACCEPTANCE_SHA,
                   canary_injected=getattr(ops, 'canary_injected', False), full_CI_or_JIT_accepted=False)
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--failure-canary', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply and not args.failure_canary:
        print(json.dumps({'mode': 'plan_only', 'native_actions': False,
                          'apply_authorized': CUTOVER_ENABLED, 'qualification_acceptance_sha256': QUALIFICATION_ACCEPTANCE_SHA,
                          'operation': OPERATION, 'failure_canary_operation': CANARY_OPERATION,
                          'planned_qualification_operation': QUALIFICATION_OPERATION,
                          'qualification_schema_version': 1, 'qualification_verdict': QUALIFICATION_VERDICT,
                          'old_GOLDEN': OLD_GOLDEN, 'new_GOLDEN': NEW_GOLDEN,
                          'initial_service': INITIAL_SERVICE, 'unchanged_pair': PAIR,
                          'only_typed_change': ['EnvironmentVariables.GOLDEN', 'EnvironmentVariables.ADDITIONAL_LABEL']}))
        return 0
    try:
        outcome = execute('failure-canary' if args.failure_canary else 'apply')
    except BaseException as error:
        outcome = {'return_code': error_code(error), 'preflight_failure_type': type(error).__name__,
                   'failure_reason': str(error) if type(error) is RecoveryError else 'private_preflight_failure',
                   'switched': False, 'admission_restored': False}
    print(json.dumps(outcome), flush=True)
    return outcome['return_code']


if __name__ == '__main__':
    raise SystemExit(main())
