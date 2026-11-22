#!/usr/bin/env python3
"""Run extracted production provisioning/loop code with offline command doubles."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--source', type=Path)
options, unittest_args = parser.parse_known_args()
SOURCE = options.source or Path(__file__).resolve().parents[1] / 'mac-ci-supervisor.sh'
BASH = '/bin/bash' if sys.platform == 'darwin' else shutil.which('bash')


def extract_function(text, name):
    match = re.search(r'^' + re.escape(name) + r'\(\) \{[^\n]*\n', text, re.MULTILINE)
    if match is None:
        raise AssertionError('production function missing: ' + name)
    if match.group().rstrip().endswith('}'):
        return match.group()
    end = re.search(r'^}\s*$', text[match.end():], re.MULTILINE)
    if end is None:
        raise AssertionError('production function boundary missing: ' + name)
    return text[match.start():match.end() + end.end()] + '\n'


@unittest.skipUnless(os.name == 'posix' and BASH, 'requires a POSIX Bash host')
class SupervisorExitStatusTests(unittest.TestCase):
    def invoke(self, *, runner=0, writer=0, stop=0, delete=0,
               start=0, missing_jit=False, drain=False, loop=False, queued=True,
               harvest=0, record_harvest=False, ssh_observations=None, inherited_ssh=None):
        text = SOURCE.read_text()
        with tempfile.TemporaryDirectory(prefix='sa-supervisor-status-') as folder:
            directory = Path(folder)
            request = directory / 'maintenance.drain'
            if drain:
                request.touch()
            code = '''set -u
GOLDEN=macos-ci
REPO=fixture/repository
LABEL=fixture-macos
POLL=.01
SUPERVISOR_DIR="$HARVEST_FIXTURE_DIR"
python3() {
  case "$1" in
    "$SUPERVISOR_DIR/preserve-failed-instance.py")
      [ "$#" -eq 4 ] || return 97
      if [ "$RECORD_HARVEST" = 1 ]; then printf 'harvest:%s:%s\\n' "$3" "$4" >> "$EVENTS"; fi
      return "$HARVEST_RC";;
    -c) "$TEST_PYTHON" "$@";;
    *) return 98;;
  esac
}
log() { printf '%s\\n' "$*"; }
unlock_keychain() { printf 'unlock\\n' >> "$EVENTS"; }
gh_api() {
  printf 'jit\\n' >> "$EVENTS"
  [ "$MISSING_JIT" = 1 ] || printf '%s\\n' '{"encoded_jit_config":"eyIucnVubmVyIjoiZTMwPSJ9"}'
}
limactl() {
  printf '%s\\n' "$1" >> "$EVENTS"
  if [ "$RECORD_SSH" = 1 ]; then "$TEST_PYTHON" "$SSH_OBSERVER" "$@" || return 98; fi
  case "$1" in
    clone) return 0;;
    start) return "$START_RC";;
    shell) printf 'synthetic runner output\\n'; return "$RUNNER_RC";;
    stop) return "$STOP_RC";;
    delete) return "$DELETE_RC";;
    *) return 99;;
  esac
}
sed() {
  printf 'writer\\n' >> "$EVENTS"
  command sed "$@"
  return "$WRITER_RC"
}
host_unhealthy() { return 1; }
human_active() { return 1; }
queued_macos_job() {
  printf 'queued\\n' >> "$EVENTS"
  if [ "$QUEUED" = 1 ] && [ ! -e "$CONSUMED" ]; then touch "$CONSUMED"; return 0; fi
  return 1
}
sleep() { printf 'sleep\\n' >> "$EVENTS"; exit 99; }
'''
            code += extract_function(text, 'draining')
            code += extract_function(text, 'provision_and_run')
            if loop:
                marker = '\nwhile true; do\n'
                self.assertEqual(text.count(marker), 1)
                code += text[text.index(marker):]
            else:
                code += 'provision_and_run\nresult=$?\nprintf "RESULT=%s\\n" "$result"\nexit "$result"\n'
            executable = directory / 'extracted-supervisor.sh'
            executable.write_text(code)
            events = directory / 'events'
            ssh_log = directory / 'ssh-observations.jsonl'
            observer = directory / 'observe-ssh.py'
            if ssh_observations is not None:
                observer.write_text("import json,os,shlex,sys\nvalue=os.environ.get('SSH')\nrow={'action':sys.argv[1],'argv':sys.argv[1:],'SSH':value,'parsed_SSH':None if value is None else shlex.split(value)}\nwith open(os.environ['SSH_OBSERVATIONS'],'a') as stream:stream.write(json.dumps(row)+'\\n')\n")
            env = dict(PATH=os.defpath, LC_ALL='C', EVENTS=str(events), DRAIN_FILE=str(request),
                       CONSUMED=str(directory / 'consumed'), RUNNER_RC=str(runner),
                       WRITER_RC=str(writer), STOP_RC=str(stop), DELETE_RC=str(delete),
                       START_RC=str(start), MISSING_JIT='1' if missing_jit else '0',
                       QUEUED='1' if queued else '0', HARVEST_FIXTURE_DIR=str(directory),
                       HARVEST_RC=str(harvest), RECORD_HARVEST='1' if record_harvest else '0',
                       TEST_PYTHON=sys.executable, RECORD_SSH='1' if ssh_observations is not None else '0',
                       SSH_OBSERVER=str(observer), SSH_OBSERVATIONS=str(ssh_log))
            if inherited_ssh is not None:
                env['SSH'] = inherited_ssh
            process = subprocess.run([BASH, str(executable)], env=env,
                                     capture_output=True, text=True, timeout=5)
            if ssh_observations is not None and ssh_log.exists():
                ssh_observations.extend(json.loads(line) for line in ssh_log.read_text().splitlines())
            return process.returncode, process.stdout, events.read_text().splitlines() if events.exists() else []

    def completed(self, expected, **options):
        status, output, events = self.invoke(**options)
        self.assertEqual(status, expected, output)
        self.assertEqual(events.count('stop'), 1, events)
        self.assertEqual(events.count('delete'), 1, events)
        self.assertEqual(events[-2:], ['stop', 'delete'])
        for label, code in (('runner', options.get('runner', 0)), ('log writer', options.get('writer', 0))):
            if code:
                self.assertRegex(output, r'\[run\] ' + label + r' FAILED .*rc=' + str(code))
        for label in ('stop', 'delete'):
            if options.get(label, 0):
                self.assertRegex(output, r'\[cleanup\] ' + label + r' FAILED .*rc=' + str(options[label]))

    def test_success_still_cleans_up(self):
        self.completed(0)

    def test_runner_failure_survives_successful_writer_and_cleanup(self):
        self.completed(17, runner=17)

    def test_writer_failure_survives_successful_cleanup(self):
        self.completed(7, writer=7)

    def test_runner_failure_precedes_writer_failure(self):
        self.completed(17, runner=17, writer=7)

    def test_stop_failure_survives_successful_delete(self):
        self.completed(5, stop=5)

    def test_delete_failure_is_returned_after_successful_run(self):
        self.completed(6, delete=6)

    def test_stop_failure_precedes_delete_failure(self):
        self.completed(5, stop=5, delete=6)

    def test_runner_failure_precedes_both_cleanup_failures(self):
        self.completed(17, runner=17, stop=5, delete=6)

    def test_writer_failure_precedes_both_cleanup_failures(self):
        self.completed(7, writer=7, stop=5, delete=6)

    def test_drain_still_prevents_provisioning(self):
        status, output, events = self.invoke(drain=True)
        self.assertEqual(status, 0)
        self.assertIn('drain', output)
        self.assertEqual(events, [])

    def test_no_queued_job_never_provisions(self):
        status, _, events = self.invoke(loop=True, queued=False)
        self.assertEqual(status, 99)  # Only the bounded fixture sleep exits.
        self.assertEqual(events, ['queued', 'sleep'])

    def test_loop_logs_failed_lifecycle_and_keeps_watching(self):
        status, output, events = self.invoke(loop=True, runner=17)
        self.assertEqual(status, 99)
        self.assertIn('[lifecycle] FAILED rc=17', output)
        self.assertEqual(events.count('queued'), 1)
        self.assertEqual(events.count('shell'), 1)
        self.assertEqual(events[-3:], ['stop', 'delete', 'sleep'])

    def test_start_failure_logs_failed_delete_without_adding_stop(self):
        status, output, events = self.invoke(start=4, delete=6)
        self.assertEqual(status, 1)
        self.assertIn('start failed', output)
        self.assertRegex(output, r'\[cleanup\] delete FAILED .*rc=6')
        self.assertEqual(events, ['clone', 'unlock', 'start', 'delete'])

    def test_missing_jit_preserves_failure_and_logs_both_cleanup_failures(self):
        status, output, events = self.invoke(missing_jit=True, stop=5, delete=6)
        self.assertEqual(status, 1)
        self.assertIn('jitconfig failed', output)
        self.assertRegex(output, r'\[cleanup\] stop FAILED .*rc=5')
        self.assertRegex(output, r'\[cleanup\] delete FAILED .*rc=6')
        self.assertEqual(events, ['clone', 'unlock', 'start', 'jit', 'stop', 'delete'])


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *unittest_args])
