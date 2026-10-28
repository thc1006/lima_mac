"""One reviewed HOST transaction; default mode only prints its exact plan.

The canary fails after verified old-service bootout and before pair replacement.
It must recover the old pair under a new PID while retaining return code 37.
Re-running this source cannot reuse its existing private operation directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

INSTALLER_SHA = '7b20f02bb0c3a684626cb8636431379a7d48bcf7253f3ab7d947a88440b9341a'
ENGINE_SHA = '10b660836aad016657cb65e7d784150ad9087e453065101db7cd7e95f28c60b2'
APPROVED_MAIN = 'a5e2f8af49cd95528b4d9c6fb754be42b5b68e2a'
APPROVED_RUN = 40611819616
OPERATION = 'supervisor-install-16b72f3a4f7346c99b576a2b07213d6b'
APPLY_AUTHORIZED = False  # Canary first; fresh old-source PID/start needs a new reviewed revision.
REPOSITORY = 'nycu-life/super-app'
STATUSES = ('queued', 'in_progress', 'waiting', 'pending', 'requested')


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def read_owned(path, *, private=False, limit=256 * 1024):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and not path.is_symlink()
            and before.st_uid == 501 and before.st_nlink == 1
            and not before.st_mode & 0o022 and before.st_size <= limit,
            'protected_regular_file_required')
    if private:
        require(stat.S_IMODE(before.st_mode) == 0o600, 'private_file_required')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), 'opened_file_changed')
        raw = os.read(descriptor, limit + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    fields = lambda value: (value.st_dev, value.st_ino, value.st_uid, value.st_nlink,
                            value.st_mode, value.st_size, value.st_mtime_ns)
    require(len(raw) == before.st_size and fields(before) == fields(after) == fields(current),
            'protected_file_changed_during_read')
    return raw


def bounded_metadata_child(arguments, environment, *, deadline):
    """Bound both pipes in flight; on any failure reap only this owned child."""
    command_deadline = min(deadline, time.monotonic() + 8)
    require(command_deadline > time.monotonic(), 'complete_queue_time_budget_exhausted')
    process = None
    captured = {'stdout': bytearray(), 'stderr': bytearray()}
    limits = {'stdout': 4 * 1024 * 1024, 'stderr': 64 * 1024}
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment)
        with selectors.DefaultSelector() as selector:
            for name, pipe in (('stdout', process.stdout), ('stderr', process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = command_deadline - time.monotonic()
                require(remaining > 0, 'bounded_metadata_command_deadline')
                for key, _ in selector.select(min(remaining, 0.1)):
                    name = key.data
                    try:
                        chunk = os.read(key.fd, min(65536, limits[name] - len(captured[name]) + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    require(len(captured[name]) + len(chunk) <= limits[name],
                            'bounded_metadata_' + name + '_limit')
                    captured[name].extend(chunk)
        # EOF is not proof of process completion: a child may close both pipes and sleep.
        process.wait(timeout=max(0, command_deadline - time.monotonic()))
        require(time.monotonic() <= command_deadline, 'bounded_metadata_command_deadline')
        return process.returncode, bytes(captured['stdout'])
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('bounded_metadata_child_unavailable') from None
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
            finally:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()


class QueueProof:
    """Fresh metadata GETs through the supervisor's existing TOKEN_FILE source.

    No authentication configuration is written or refreshed. The existing PAT
    stays on HOST, in memory and the gh child's environment, as in gh_api().
    Neither the token, its digest, raw API output nor child stderr is recorded.
    """
    def __init__(self, home=None):
        self.home = Path.home() if home is None else Path(home)

    def api(self, suffix, environment, deadline):
        require(type(suffix) is str and bool(re.fullmatch(
                r'(git/ref/heads/main|actions/runs/[1-9][0-9]{0,19}'
                r'|actions/runs/[1-9][0-9]{0,19}/attempts/1/jobs\?per_page=100'
                r'|actions/runs/[1-9][0-9]{0,19}/jobs\?per_page=100'
                r'|actions/runs\?status=(queued|in_progress|waiting|pending|requested)&per_page=100)', suffix)),
                'allowlisted_metadata_endpoint_required')
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'complete_queue_time_budget_exhausted')
        return_code, output = bounded_metadata_child(
            ['/opt/homebrew/bin/gh', 'api', '--method', 'GET', 'repos/' + REPOSITORY + '/' + suffix],
            environment, deadline=deadline)
        require(return_code == 0, 'GitHub_metadata_GET_unavailable')
        return json.loads(output)

    @staticmethod
    def complete_page(value, key, maximum=100):
        rows = value.get(key)
        require(isinstance(rows, list) and type(value.get('total_count')) is int
                and 0 <= value['total_count'] <= maximum and value['total_count'] == len(rows),
                'complete_bounded_inventory_required')
        return rows

    def __call__(self):
        deadline = time.monotonic() + 25
        started = time.time()
        # Reuse only the established default source; a changed source needs review.
        import plistlib
        configured = plistlib.loads(read_owned(self.home / 'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist'))
        token_path = self.home / 'ci/.gh_token'
        require(configured.get('EnvironmentVariables', {}).get('TOKEN_FILE', str(token_path)) == str(token_path),
                'existing_TOKEN_FILE_source_changed')
        token = read_owned(token_path, private=True, limit=8192).strip().decode('ascii')
        require(re.fullmatch(r'[A-Za-z0-9_]{1,8192}', token), 'existing_token_format_unavailable')
        environment = {'PATH': '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(self.home),
                       'LC_ALL': 'C', 'GH_TOKEN': token, 'GH_PROMPT_DISABLED': '1', 'GIT_TERMINAL_PROMPT': '0'}
        try:
            get = lambda suffix: self.api(suffix, environment, deadline)
            require(get('git/ref/heads/main')['object']['sha'] == APPROVED_MAIN, 'approved_main_changed')
            run = get('actions/runs/' + str(APPROVED_RUN))
            require(run.get('id') == APPROVED_RUN and run.get('head_sha') == APPROVED_MAIN and run.get('workflow_id') == 331308620
                    and run.get('event') == 'push' and run.get('head_branch') == 'main'
                    and run.get('run_attempt') == 1 and run.get('status') == 'completed'
                    and run.get('conclusion') == 'success', 'approved_main_CI_success_required')
            jobs = self.complete_page(get('actions/runs/' + str(APPROVED_RUN) + '/attempts/1/jobs?per_page=100'), 'jobs')
            by_name = {job['name']: job for job in jobs}
            require(len(by_name) == len(jobs) and all(by_name.get(name, {}).get('conclusion') == 'success'
                    for name in ('Native iOS', 'CI gate')), 'approved_native_and_gate_required')
            active = {}
            for status in STATUSES:
                rows = self.complete_page(get('actions/runs?status=' + status + '&per_page=100'), 'workflow_runs')
                require(all(type(row.get('id')) is int and 0 < row['id'] < 10**20 for row in rows),
                        'numeric_workflow_run_identity_required')
                active.update((row['id'], row) for row in rows)
            require(len(active) <= 25, 'bounded_active_inventory_required')
            matching = []
            for identifier in sorted(active):
                rows = self.complete_page(get('actions/runs/' + str(identifier) + '/jobs?per_page=100'), 'jobs')
                for row in rows:
                    require(isinstance(row, dict) and type(row.get('id')) is int
                            and 0 < row['id'] < 10**20 and row.get('status') in (*STATUSES, 'completed'),
                            'active_job_inventory_unavailable')
                    if row['status'] == 'completed':
                        continue
                    labels = row.get('labels')
                    require(isinstance(labels, list) and bool(labels)
                            and all(type(label) is str and bool(label.strip()) for label in labels),
                            'active_job_inventory_unavailable')
                    if any('macos' in label.lower() for label in labels):
                        matching.append({'run': identifier, 'job': row['id'], 'status': row['status']})
            require(not matching, 'queued_or_active_Mac_job_priority')
            require(get('git/ref/heads/main')['object']['sha'] == APPROVED_MAIN, 'main_changed_during_inventory')
            latest = get('actions/runs/' + str(APPROVED_RUN))
            require(all(latest.get(key) == run.get(key) for key in
                        ('id', 'head_sha', 'workflow_id', 'run_attempt', 'status', 'conclusion')),
                    'approved_CI_changed_during_inventory')
            require(time.monotonic() <= deadline, 'complete_queue_time_budget_exhausted')
            return {'complete': True, 'repository': REPOSITORY, 'statuses_checked': list(STATUSES),
                    'observed_epoch': time.time(), 'inventory_started_epoch': started,
                    'active_runs_checked': sorted(active), 'matching_Mac_jobs': matching,
                    'approved_main': APPROVED_MAIN, 'approved_run': APPROVED_RUN}
        finally:
            environment.pop('GH_TOKEN', None)
            token = None


def load_modules():
    directory = Path(__file__).resolve().parent
    for name, expected in (('supervisor_install.py', INSTALLER_SHA), ('maintenance_recovery.py', ENGINE_SHA)):
        require(hashlib.sha256(read_owned(directory / name)).hexdigest() == expected, 'reviewed_dependency_changed')
    sys.path.insert(0, str(directory))
    from supervisor_install import InstallOperations, NEW, coordinated_install
    return InstallOperations, NEW, coordinated_install


def canary_operations(base):
    class ControlledCanary(base):
        def install_pair(self, kind):
            if kind == 'new':
                # coordinated_install has just booted out and proven old PID gone.
                self.canary_injected = True
                raise SystemExit(37)
            return super().install_pair(kind)
    return ControlledCanary


def execute(mode):
    require(sys.platform == 'darwin' and os.getuid() == os.geteuid() == 501, 'Mac_HOST_UID501_required')
    InstallOperations, new_pair, transaction = load_modules()
    home = Path.home()
    stage = home / 'ci/xcode27-guest-prep-20261027'
    require(stage.resolve() == stage and stage.is_dir() and not stage.is_symlink()
            and stat.S_IMODE(stage.stat().st_mode) == 0o700 and stage.stat().st_uid == 501,
            'existing_private_management_stage_required')
    created = json.loads(read_owned(stage / 'golden-created-stopped.json', private=True))
    paused = json.loads(read_owned(stage / 'pr216-new-golden-paused-stopped.json', private=True))
    protected = {'macos-ci': created['original_golden_storage_proof']['golden'],
                 'macos-xcode27-candidate-20261027': created['source_stopped_storage'],
                 'macos-ci-xcode27-20261027': paused['new_image_storage']}
    directory = Path(__file__).resolve().parent
    payload = {name: read_owned(directory / name) for name in new_pair}
    operation = stage / OPERATION
    operation.mkdir(mode=0o700)  # Exclusive: never replay this operation or erase its receipts.

    implementation = canary_operations(InstallOperations) if mode == 'failure-canary' else InstallOperations
    ops = implementation(operation, payload=payload, protected_storage=protected, queue_probe=QueueProof())
    result = transaction(ops)
    result.update(caller_mode=mode, operation=str(operation), approved_main=APPROVED_MAIN,
                  approved_run=APPROVED_RUN, canary_injected=getattr(ops, 'canary_injected', False))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true')
    group.add_argument('--failure-canary', action='store_true')
    args = parser.parse_args()
    if not args.apply and not args.failure_canary:
        print(json.dumps({'mode': 'plan_only', 'approved_main': APPROVED_MAIN,
                          'approved_run': APPROVED_RUN, 'operation': OPERATION,
                          'initial_PID': 1872, 'native_actions': False, 'apply_authorized': APPLY_AUTHORIZED,
                          'failure_canary': 'after verified old bootout, before replacement; retain RC37',
                          'credentials': 'existing HOST TOKEN_FILE, native child memory only; no auth reconfiguration'}))
        return 0
    if args.apply and not APPLY_AUTHORIZED:
        print(json.dumps({'return_code': 1, 'reason': 'post_canary_identity_and_apply_source_review_required',
                          'native_actions': False}), flush=True)
        return 1
    try:
        result = execute('failure-canary' if args.failure_canary else 'apply')
    except Exception as error:
        result = {'return_code': 1, 'preflight_failure_type': type(error).__name__,
                  'reason': 'inspect_private_operation_before_any_retry'}
    print(json.dumps(result), flush=True)
    return result['return_code']


if __name__ == '__main__':
    raise SystemExit(main())
