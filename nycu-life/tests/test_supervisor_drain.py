#!/usr/bin/env python3
"""Exercise the real supervisor loop with disposable command/VM fixtures."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'mac-ci-supervisor.sh'
TEST_BASH = '/bin/bash' if sys.platform == 'darwin' else shutil.which('bash')


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'requires a POSIX host')
class SupervisorDrainTests(unittest.TestCase):
    @contextmanager
    def supervisor(self, *, drain=False, broken_link=False, lookup_drain=False,
                   permanently_queued=False, instances=''):
        with tempfile.TemporaryDirectory(prefix='sa-supervisor-fixture-') as name:
            directory = Path(name)
            tools = directory / 'tools'
            tools.mkdir()
            request = directory / 'maintenance.drain'
            if drain:
                request.touch()
            if broken_link:
                request.symlink_to(directory / 'missing')
            (directory / 'instances').write_text(instances)
            (directory / 'token').write_text('disposable-test-token')
            (directory / 'password').write_text('disposable-test-password')
            for command, body in {
                'df': "printf 'Filesystem 1G-blocks Used Available Capacity Mounted\\nfixture 1000 1 999 1%% /\\n'\n",
                'ioreg': 'exit 0\n',
                'security': 'printf "security\\n" >> "$FIXTURE/events"\n',
                'gh': '''printf 'gh %s\\n' "$*" >> "$FIXTURE/events"
case "$*" in
  *generate-jitconfig*) touch "$FIXTURE/consumed"; printf 'disposable-jit\\n';;
  *runs/101/jobs*)
    if [ "$LOOKUP_DRAIN" = 1 ]; then touch "$DRAIN_FILE"; fi
    printf 'Native iOS\\n';;
  *actions/runs?*) if [ "$ALWAYS_QUEUED" = 1 ] || [ ! -e "$FIXTURE/consumed" ]; then printf '101\\n'; fi;;
  *) exit 2;;
esac
''',
                'limactl': '''printf '%s\\n' "$*" >> "$FIXTURE/events"
case "$1" in
  list) cat "$FIXTURE/instances";;
  clone) printf '%s\\n' "$3" >> "$FIXTURE/instances";;
  start|stop) exit 0;;
  delete)
    awk -v target="$3" '$0 != target' "$FIXTURE/instances" > "$FIXTURE/next"
    mv "$FIXTURE/next" "$FIXTURE/instances";;
  shell)
    touch "$FIXTURE/started"
    while [ ! -e "$FIXTURE/release" ]; do sleep .02; done
    touch "$FIXTURE/completed"
    printf 'fixture job completed\\n';;
  *) exit 2;;
esac
''',
            }.items():
                path = tools / command
                path.write_text('#!/bin/bash\nset -eu\n' + body)
                path.chmod(0o700)
            original = SOURCE.read_text()
            export = 'export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
            self.assertEqual(original.count(export), 1)
            executable = directory / 'supervisor.sh'
            executable.write_text(original.replace(export, f'export PATH="{tools}:/usr/bin:/bin:/usr/sbin:/sbin"'))
            env = dict(os.environ, FIXTURE=str(directory), DRAIN_FILE=str(request),
                       TOKEN_FILE=str(directory / 'token'), KC_PW_FILE=str(directory / 'password'),
                       ALWAYS_QUEUED='1' if permanently_queued else '0',
                       LOOKUP_DRAIN='1' if lookup_drain else '0', POLL='.02')
            with (directory / 'log').open('w') as output:
                process = subprocess.Popen([TEST_BASH, str(executable)], env=env,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    yield directory, request
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=3)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('bounded fixture observation timed out')

    @staticmethod
    def events(directory):
        path = directory / 'events'
        return path.read_text().splitlines() if path.exists() else []

    def test_startup_drain_precedes_orphan_reconciliation_and_dispatch(self):
        with self.supervisor(drain=True, instances='macos-job-retained\n') as (directory, _):
            self.wait_for(lambda: (directory / 'log').read_text().count('startup paused') >= 5)
            self.assertEqual(self.events(directory), [])
            self.assertFalse((directory / 'consumed').exists())

    def test_broken_symlink_also_keeps_startup_paused(self):
        with self.supervisor(broken_link=True) as (directory, _):
            self.wait_for(lambda: (directory / 'log').read_text().count('startup paused') >= 5)
            self.assertEqual(self.events(directory), [])

    def test_removing_startup_request_resumes_normal_dispatch(self):
        with self.supervisor(drain=True) as (directory, request):
            self.wait_for(lambda: 'startup paused' in (directory / 'log').read_text())
            request.unlink()
            self.wait_for(lambda: (directory / 'started').exists())
            self.assertEqual(sum(event.startswith('clone ') for event in self.events(directory)), 1)

    def test_request_during_github_lookup_prevents_clone(self):
        with self.supervisor(lookup_drain=True) as (directory, _):
            self.wait_for(lambda: 'skip queued job' in (directory / 'log').read_text())
            self.assertFalse(any(event.startswith('clone ') for event in self.events(directory)))
            self.assertFalse((directory / 'started').exists())

    def test_request_during_job_preserves_completion_and_cleanup(self):
        with self.supervisor(permanently_queued=True) as (directory, request):
            self.wait_for(lambda: (directory / 'started').exists())
            request.touch()
            github_calls = sum(event.startswith('gh ') for event in self.events(directory))
            self.assertFalse(any(event.startswith('stop ') for event in self.events(directory)))
            (directory / 'release').touch()
            self.wait_for(lambda: (directory / 'log').read_text().count('[admission] drain — idle') >= 5)
            self.assertTrue((directory / 'completed').exists())
            events = self.events(directory)
            self.assertEqual(sum(event.startswith('clone ') for event in events), 1)
            self.assertEqual(sum(event.startswith('stop -f macos-job-') for event in events), 1)
            self.assertEqual(sum(event.startswith('delete -f macos-job-') for event in events), 1)
            self.assertEqual(sum(event.startswith('gh ') for event in events), github_calls)

    def test_removing_runtime_request_allows_next_job(self):
        with self.supervisor() as (directory, request):
            self.wait_for(lambda: (directory / 'started').exists())
            request.touch()
            (directory / 'release').touch()
            self.wait_for(lambda: '[admission] drain — idle' in (directory / 'log').read_text())
            (directory / 'started').unlink()
            (directory / 'consumed').unlink()
            request.unlink()
            self.wait_for(lambda: (directory / 'started').exists())
            self.assertEqual(sum(event.startswith('clone ') for event in self.events(directory)), 2)

    def test_no_drain_keeps_existing_cleanup_and_candidate_prefix(self):
        with self.supervisor(instances='macos-job-orphan\nxcode27-candidate\n') as (directory, _):
            self.wait_for(lambda: (directory / 'started').exists())
            (directory / 'release').touch()
            self.wait_for(lambda: sum(event.startswith('delete -f macos-job-') for event in self.events(directory)) == 2)
            events = self.events(directory)
            self.assertIn('stop -f macos-job-orphan', events)
            self.assertIn('delete -f macos-job-orphan', events)
            self.assertFalse(any('xcode27-candidate' in event for event in events))


if __name__ == '__main__':
    unittest.main()
