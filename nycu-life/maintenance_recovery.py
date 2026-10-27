"""Pre-cutover maintenance failure recovery; never turn a failed phase green.

The caller supplies reviewed storage/control proofs and an owned drain-creation
record. The phase callback runs under the existing maintenance lock and must use
bounded native commands. No runner/job cancellation, force, delete or cutover.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import tempfile
import time


class RecoveryError(RuntimeError):
    pass


class MaintenanceTermination(BaseException):
    def __init__(self, number):
        self.number = number


@contextmanager
def termination_boundary():
    previous = {}
    def interrupted(number, _frame):
        raise MaintenanceTermination(number)
    try:
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def require(value, reason):
    if not value:
        raise RecoveryError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(storage):
    return {'lima.yaml': storage['lima.yaml'],
            **{name: {key: storage[name][key] for key in ('device', 'inode', 'links')}
               for name in ('disk', 'vz-aux')}}


def _validate(ops, proof, *, all_stopped, marker=True):
    require(proof['control']['GOLDEN'] == proof['original_golden'], 'pre_cutover_original_GOLDEN_required')
    if proof.get('expected_initial_owned_state') == 'Running':
        require(proof.get('ownership_operation') == ops.ownership_operation,
                'running_followup_original_ownership_required')
    facts = ops.observe(proof)
    require(facts['original_storage'] == proof['original_storage'], 'original_storage_changed')
    require(facts['source_storage'] == proof['source_storage'], 'source_storage_changed')
    require(identity(facts['owned_storage']) == proof['owned_identity'], 'owned_VM_identity_changed')
    require(facts['control'] == proof['control'], 'original_runner_control_changed')
    require(facts['original_state'] == facts['source_state'] == 'Stopped', 'original_or_source_not_stopped')
    require(not facts['job_VMs'] and not facts['other_active_VMs'], 'other_VM_or_job_present')
    require(facts['owned_state'] in (('Stopped',) if all_stopped else ('Running', 'Stopped')),
            'owned_VM_state_unknown_or_busy')
    if marker:
        require(ops.marker_identity() == proof['marker_identity'], 'owned_drain_changed')
        require(ops.creation_record_hash() == proof['creation_record_sha256'], 'drain_creation_record_changed')
    return facts


def guarded_phase(ops, proof, phase):
    """Run one bounded callback. A recovered failure still returns its original RC.

    The shared lock stays held through preparation, phase and recovery. ``proof``
    may be a READ-ONLY callback. A proof with ``create_owned_drain=True`` delegates
    drain creation to the engine under the recovery boundary. Neither callback
    may reacquire the lock. All returned evidence is nonsensitive. Hard kill and
    power loss cannot run Python cleanup; retain their durable intents for review.
    """
    outcome = {'phase_return_code': None, 'return_code': 1, 'recovered': False, 'admission_restored': False,
               'phase_started': False, 'recovery_attempted': False, 'qualified_image': False,
               'job_or_CI_success_claimed': False, 'evidence_unavailable': []}
    def audit(name, value):
        try:
            ops.record(name, value)
            return True
        except Exception:
            if name not in outcome['evidence_unavailable']:
                outcome['evidence_unavailable'].append(name)
            return False
    try:
        with termination_boundary(), ops.lock():
            try:
                if callable(proof):
                    proof = proof()
                initial = proof.get('expected_initial_owned_state', 'Stopped')
                require(initial in ('Stopped', 'Running'), 'invalid_initial_owned_state')
                if proof.get('create_owned_drain') is True:
                    require(initial == 'Stopped', 'running_followup_cannot_create_or_adopt_drain')
                    _validate(ops, proof, all_stopped=True, marker=False)
                    ops.create_owned_drain(proof)
                facts = _validate(ops, proof, all_stopped=initial == 'Stopped')
                require(facts['owned_state'] == initial, 'expected_initial_owned_state_not_observed')
                if initial == 'Running':
                    require(ops.native_quiet(proof['owned_vm']), 'owned_native_work_present_or_unknown')
                ops.wait_idle(proof['control'], timeout=35)
                ops.record('armed.json', {'proof': proof, 'phase_source_sha256': proof['phase_source_sha256']})
                outcome['phase_started'] = True
                rc = phase()
                require(type(rc) is int and 0 <= rc <= 255, 'invalid_phase_return_code')
            except KeyboardInterrupt:
                rc = 130
                outcome['phase_failure_type'] = 'KeyboardInterrupt'
            except MaintenanceTermination as error:
                rc = 128 + error.number
                outcome['phase_failure_type'] = 'normal_termination_signal'
            except SystemExit as error:
                rc = error.code if type(error.code) is int and 0 <= error.code <= 255 else (0 if error.code is None else 1)
                outcome['phase_failure_type'] = 'SystemExit'
            except BaseException as error:
                rc = 1
                outcome['phase_failure_type'] = type(error).__name__
                if type(error) is RecoveryError:
                    outcome['phase_failure_reason'] = str(error)
            outcome['phase_return_code'] = rc
            outcome['return_code'] = rc
            if rc == 0:
                if audit('phase-outcome.json', outcome):
                    return outcome
                outcome['return_code'] = 1
                outcome['phase_evidence_write_failed'] = True
            outcome['recovery_attempted'] = True
            try:
                # Durable creation/ownership proofs remain mandatory below.
                # A full disk must not make optional later audit writes strand
                # the verified owned drain. Report every missing record.
                audit('phase-failure.json', outcome)
                facts = _validate(ops, proof, all_stopped=False)
                if facts['owned_state'] == 'Running':
                    require(ops.native_quiet(proof['owned_vm']), 'owned_native_work_present_or_unknown')
                    audit('normal-stop-intent.json', {'VM': proof['owned_vm'], 'phase_return_code': rc})
                    ops.normal_stop(proof['owned_vm'], timeout=180)
                _validate(ops, proof, all_stopped=True)
                ops.wait_idle(proof['control'], timeout=35)
                _validate(ops, proof, all_stopped=True)
                audit('drain-removal-intent.json', {'marker_identity': proof['marker_identity'],
                                                  'phase_return_code': rc})
                require(ops.marker_identity() == proof['marker_identity'], 'owned_drain_changed_before_unlink')
                require(ops.creation_record_hash() == proof['creation_record_sha256'], 'drain_creation_record_changed_before_unlink')
                ops.remove_marker()
                require(not ops.marker_present(), 'owned_drain_removal_not_verified')
                # Admission may immediately create a healthy job: never stop it.
                ops.verify_preserved_after_resume(proof)
                outcome.update(recovered=True, admission_restored=True)
            except BaseException as error:
                outcome['recovery_failure_type'] = type(error).__name__
                outcome['recovery_reason'] = str(error) if type(error) is RecoveryError else 'native_failure_details_suppressed'
            if not audit('phase-outcome.json', outcome):
                outcome['recovery_evidence_write_failed'] = True
    except BaseException as error:
        if outcome['phase_return_code'] is None:
            outcome['phase_return_code'] = 1
        if outcome['return_code'] == 0:
            outcome['return_code'] = 1
        outcome['preflight_or_lock_failure_type'] = type(error).__name__
        outcome['preflight_reason'] = str(error) if type(error) is RecoveryError else 'native_failure_details_suppressed'
    return outcome


class MacOperations:
    """Native adapter. Caller-created operation/record paths are private and pinned."""
    def __init__(self, operation, creation_record, creation_sha256, *, lock_path, home=None,
                 expected_device_count=12, marker_content=None, ownership_operation=None):
        self.home = Path.home() if home is None else Path(home)
        self.operation = Path(operation)
        self.creation_record = Path(creation_record)
        self.creation_sha256 = creation_sha256
        self.ownership_operation = self.operation.name if ownership_operation is None else ownership_operation
        require(type(self.ownership_operation) is str
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.ownership_operation),
                'invalid_ownership_operation')
        self.marker_content = (self.operation.name + '\n').encode() if marker_content is None else marker_content
        require(type(self.marker_content) is bytes and 0 < len(self.marker_content) <= 1024,
                'bounded_owned_marker_content_required')
        self.lima = '/opt/homebrew/bin/limactl'
        self.marker = self.home / 'ci/maintenance.drain'
        self.shared_lock = Path(lock_path)
        self.expected_device_count = expected_device_count
        self.label = 'gui/501/com.nycu-life.mac-ci-supervisor'
        self.log = self.home / 'ci/supervisor.log'

    def _run(self, args, timeout=20):
        try:
            return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise RecoveryError('bounded_native_command_unavailable') from None

    def _regular(self, path, private=False):
        value = path.lstat()
        require(stat.S_ISREG(value.st_mode) and not path.is_symlink() and value.st_uid == 501
                and value.st_nlink == 1, 'private_file_identity_invalid')
        if private:
            require(stat.S_IMODE(value.st_mode) == 0o600, 'private_file_mode_invalid')
        return value

    @contextmanager
    def lock(self):
        require(os.getuid() == os.geteuid() == 501, 'HOST_UID501_required')
        folder = self.operation.lstat()
        require(stat.S_ISDIR(folder.st_mode) and not self.operation.is_symlink()
                and self.operation.resolve() == self.operation and folder.st_uid == 501
                and stat.S_IMODE(folder.st_mode) == 0o700, 'private_operation_invalid')
        expected = self._regular(self.shared_lock, True)
        descriptor = os.open(self.shared_lock, os.O_RDWR | os.O_NOFOLLOW)
        try:
            value = os.fstat(descriptor)
            require(stat.S_ISREG(value.st_mode) and value.st_uid == 501 and value.st_nlink == 1
                    and stat.S_IMODE(value.st_mode) == 0o600
                    and (value.st_dev, value.st_ino) == (expected.st_dev, expected.st_ino),
                    'opened_shared_lock_changed')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoveryError('exclusive_maintenance_lock_busy') from None
            current = self._regular(self.shared_lock, True)
            require((current.st_dev, current.st_ino) == (value.st_dev, value.st_ino),
                    'shared_lock_path_replaced')
            yield
        finally:
            os.close(descriptor)

    def record(self, name, value):
        require(re.fullmatch(r'[a-z-]+\.json', name), 'invalid_record_name')
        descriptor, temporary = tempfile.mkstemp(prefix='.record-', dir=self.operation)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write((json.dumps(value, indent=2) + '\n').encode())
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic publication without replacing a prior attempt or evidence.
            os.link(temporary, self.operation / name, follow_symlinks=False)
        finally:
            os.unlink(temporary)

    def marker_identity(self):
        value = self._regular(self.marker, True)
        raw = self.marker.read_bytes()
        return {'device': value.st_dev, 'inode': value.st_ino, 'owner': value.st_uid,
                'mode': stat.S_IMODE(value.st_mode), 'links': value.st_nlink,
                'size': value.st_size, 'mtime_ns': value.st_mtime_ns, 'sha256': digest(raw)}

    def private_relative_record(self, directory, name):
        """Resolve an allowlisted receipt under real private owned ancestors."""
        require(type(name) is str and len(name) <= 512, 'invalid_relative_record_path')
        parts = name.split('/')
        require(1 <= len(parts) <= 8 and all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', part)
                and part not in ('.', '..') for part in parts), 'invalid_relative_record_path')
        directory = Path(directory)
        require(directory.is_absolute() and directory.resolve() == directory, 'record_root_changed')
        for ancestor in (directory, *(directory.joinpath(*parts[:index]) for index in range(1, len(parts)))):
            value = ancestor.lstat()
            require(stat.S_ISDIR(value.st_mode) and not ancestor.is_symlink() and value.st_uid == 501
                    and stat.S_IMODE(value.st_mode) == 0o700, 'record_ancestor_not_private_owned_directory')
        path = directory.joinpath(*parts)
        self._regular(path, True)
        return path

    def creation_record_hash(self):
        self._regular(self.creation_record, True)
        raw = self.creation_record.read_bytes()
        require(digest(raw) == self.creation_sha256, 'drain_creation_record_changed')
        record = json.loads(raw)
        require(record.get('operation') == self.ownership_operation and record.get('drain_path') == str(self.marker),
                'drain_not_owned_by_operation')
        if record.get('kind') == 'owned_drain_creation_intent':
            require(record.get('planned_marker_sha256') == digest(self.marker_content),
                    'drain_creation_intent_changed')
        else:
            require(record.get('marker_identity') == self.marker_identity(), 'drain_not_owned_by_operation')
        return digest(raw)

    def create_owned_drain(self, proof):
        """Publish durable ownership intent, then create only an absent marker.

        Proof identity is captured from the successfully O_EXCL-created file even
        if writing/fsync or the later evidence record fails. An unknown identity
        fails recovery closed. No prior/operator marker is adopted.
        """
        require(not self.marker_present(), 'unexpected_existing_drain')
        require(self.ownership_operation == self.operation.name, 'new_drain_requires_this_operation_ownership')
        raw = self.marker_content
        self.record('drain-creation-intent.json', {'operation': self.operation.name,
                    'kind': 'owned_drain_creation_intent', 'drain_path': str(self.marker),
                    'planned_marker_sha256': digest(raw), 'proof': proof})
        self.creation_record = self.operation / 'drain-creation-intent.json'
        self.creation_sha256 = digest(self.creation_record.read_bytes())
        proof['creation_record_sha256'] = self.creation_sha256
        descriptor = os.open(self.marker, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, 'r+b', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            try:
                created = os.fstat(descriptor)
                current = self._regular(self.marker, True)
                require((created.st_dev, created.st_ino) == (current.st_dev, current.st_ino),
                        'created_drain_identity_changed')
                proof['marker_identity'] = self.marker_identity()
            finally:
                os.close(descriptor)
        self.record('drain-created.json', {'operation': self.operation.name,
                    'marker_identity': proof['marker_identity'], 'creation_intent_sha256': self.creation_sha256})

    def storage(self, name):
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name), 'invalid_VM_name')
        result = {}
        for key in ('lima.yaml', 'disk', 'vz-aux'):
            path = self.home / '.lima' / name / key
            value = self._regular(path)
            result[key] = {'device': value.st_dev, 'inode': value.st_ino, 'links': value.st_nlink,
                           'size': value.st_size, 'mtime_ns': value.st_mtime_ns}
        result['lima.yaml']['sha256'] = digest((self.home / '.lima' / name / 'lima.yaml').read_bytes())
        return result

    def _service(self):
        disabled = self._run(['/bin/launchctl', 'print-disabled', 'gui/501'])
        require(disabled.returncode == 0, 'original_runner_disabled_status_unknown')
        values = re.findall(r'(?m)^\s*"com\.nycu-life\.mac-ci-supervisor"\s*=>\s*(true|false)\s*$',
                            disabled.stdout.decode())
        require(len(values) <= 1 and values != ['true'], 'original_runner_service_disabled')
        result = self._run(['/bin/launchctl', 'print', self.label])
        require(result.returncode == 0, 'original_runner_service_unavailable')
        text = result.stdout.decode()
        # launchctl nests resource/association records with their own state and
        # PID fields. Only the one-tab service fields identify this process.
        state = re.findall(r'(?m)^\tstate = (\w+)[ \t]*$', text)
        pid = re.findall(r'(?m)^\tpid = ([0-9]+)[ \t]*$', text)
        golden = re.findall(r'(?m)^\s*"?GOLDEN"?\s*(?:=>|=)\s*"?([^"\s]+)"?\s*$', text)
        require(state == ['running'] and len(pid) == 1 and len(golden) <= 1, 'original_runner_service_unknown')
        start = self._run(['/bin/ps', '-p', pid[0], '-o', 'lstart='])
        require(start.returncode == 0 and bool(start.stdout.strip()), 'original_runner_process_unknown')
        return {'pid': int(pid[0]), 'start': start.stdout.decode().strip(),
                'GOLDEN': golden[0] if golden else 'macos-ci'}

    def control(self):
        live = self.home / 'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist'
        script = self.home / 'ci/mac-ci-supervisor.sh'
        self._regular(live)
        self._regular(script)
        raw = live.read_bytes()
        configured = plistlib.loads(raw).get('EnvironmentVariables', {}).get('GOLDEN', 'macos-ci')
        service = self._service()
        require(configured == service['GOLDEN'], 'effective_GOLDEN_differs')
        return {'plist_sha256': digest(raw), 'supervisor_sha256': digest(script.read_bytes()),
                'GOLDEN': configured, 'service': service}

    def observe(self, proof):
        owned, original, source = (proof[k] for k in ('owned_vm', 'original_golden', 'source_vm'))
        require(len({owned, original, source}) == 3 and not owned.startswith('macos-job-'), 'owned_VM_scope_invalid')
        result = self._run([self.lima, 'list', '--json'])
        require(result.returncode == 0, 'VM_inventory_unavailable')
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        require(len({r['name'] for r in rows}) == len(rows), 'duplicate_VM_inventory')
        states = {r['name']: r['status'] for r in rows}
        require(all(name in states for name in (owned, original, source)), 'protected_VM_missing')
        return {'owned_state': states[owned], 'original_state': states[original], 'source_state': states[source],
                'job_VMs': [name for name in states if name.startswith('macos-job-')],
                'other_active_VMs': [name for name, state in states.items() if name not in (owned, original, source) and state != 'Stopped'],
                'owned_storage': self.storage(owned), 'original_storage': self.storage(original),
                'source_storage': self.storage(source), 'control': self.control()}

    def wait_idle(self, control, timeout):
        deadline = time.monotonic() + timeout
        start = self._regular(self.log).st_size
        inode = self.log.stat().st_ino
        while time.monotonic() < deadline:
            require(self.control() == control, 'original_runner_control_changed')
            result = self._run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,comm='])
            require(result.returncode == 0, 'native_process_inventory_failed')
            rows = [line.split(None, 3) for line in result.stdout.decode().splitlines() if line.strip()]
            require(all(len(row) == 4 and all(v.isdigit() for v in row[:3]) for row in rows), 'native_process_inventory_malformed')
            pid = control['service']['pid']
            require(any(int(r[0]) == pid and int(r[2]) == pid for r in rows), 'original_runner_group_changed')
            descendants = {pid}
            while True:
                new = descendants | {int(r[0]) for r in rows if int(r[1]) in descendants}
                if new == descendants:
                    break
                descendants = new
            quiet = all(r[3] in ('sleep', '/bin/sleep') for r in rows if int(r[0]) in descendants and int(r[0]) != pid)
            require(self.log.stat().st_ino == inode, 'supervisor_log_replaced')
            with self.log.open('rb') as stream:
                stream.seek(start)
                tail = stream.read(1024 * 1024)
            if quiet and any(b'[admission] drain' in line and (b'idle' in line or b'startup paused' in line) for line in tail.splitlines()):
                return
            time.sleep(.25)
        raise RecoveryError('original_runner_idle_not_verified')

    def native_quiet(self, name):
        source = 'for n in Runner.Listener Runner.Worker xcodebuild swift-frontend softwareupdate osinstallersetupd; do /usr/bin/pgrep -x "$n" >/dev/null 2>&1; r=$?; test "$r" = 1 || exit 1; done'
        result = self._run([self.lima, 'shell', name, '--', '/bin/bash', '-c', source], timeout=25)
        if result.returncode != 0:
            return False
        result = self._run([self.lima, 'shell', name, '--', '/usr/bin/env',
                            'DEVELOPER_DIR=/Applications/Xcode_27.1.app/Contents/Developer',
                            '/usr/bin/xcrun', 'simctl', 'list', '--json'], timeout=45)
        if result.returncode != 0:
            return False
        try:
            devices = json.loads(result.stdout)['devices']
            if not isinstance(devices, dict) or not all(isinstance(group, list) for group in devices.values()):
                return False
            values = [device for group in devices.values() for device in group]
            return len(values) == self.expected_device_count and all(
                isinstance(device, dict) and device.get('state') == 'Shutdown' for device in values)
        except (ValueError, KeyError, TypeError):
            return False

    def normal_stop(self, name, timeout):
        result = self._run([self.lima, 'stop', name], timeout=timeout)
        require(result.returncode == 0, 'normal_owned_VM_stop_failed')

    def remove_marker(self):
        self.marker.unlink()

    def marker_present(self):
        return self.marker.exists() or self.marker.is_symlink()

    def verify_preserved_after_resume(self, proof):
        facts = self.observe(proof)
        require(facts['owned_state'] == facts['original_state'] == facts['source_state'] == 'Stopped'
                and all(name.startswith('macos-job-') for name in facts['other_active_VMs']),
                'post_resume_protected_VM_state_changed')
        require(self.storage(proof['original_golden']) == proof['original_storage']
                and self.storage(proof['source_vm']) == proof['source_storage']
                and identity(self.storage(proof['owned_vm'])) == proof['owned_identity']
                and self.control() == proof['control'], 'post_resume_protected_state_changed')
