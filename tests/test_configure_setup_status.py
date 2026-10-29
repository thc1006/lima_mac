"""Run the exact production main/timeout declarations with inert Bash steps.

Never source configure.sh: its other declarations and entry point can read
credentials or change settings. Only the two extracted declarations execute.
"""
import argparse
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
SOURCE = options.source or Path(__file__).resolve().parents[1] / 'configure.sh'
BASH = shutil.which('bash')


def declaration(text, name):
    matches = list(re.finditer(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}\n',
                               text, re.MULTILINE | re.DOTALL))
    if len(matches) != 1:
        raise AssertionError('one exact production declaration required: ' + name)
    return matches[0].group()


DOUBLES = r'''set -u
record() { printf '%s\n' "$1" >> "$EVENTS"; }
configure_password() { record password; }
configure_autologin() { record autologin; }
configure_setup_assistant() {
    record setup
    if [ "$FIXTURE_CASE" = timeout ]; then while :; do :; done; fi
    return "$FIXTURE_SETUP_STATUS"
}
configure_screensaver() { record screensaver; }
configure_ssh_keys() { record ssh-keys; }
configure_chezmoi() { record chezmoi; }
configure_runner() { record runner; }
launchctl() { record launchctl; }
id() { printf '703\n'; }
log_info() { record "info:$*"; }
log_warn() { record "warn:$*"; }
log_error() { record "error:$*"; }
# Keep the production watcher/kill/wait code. Only its clock is accelerated.
sleep() {
    [ "$#" = 1 ] && [ "$1" = 60 ] || return 92
    /bin/sleep .04
}
OSTYPE=darwin-fixture
'''


@unittest.skipUnless(os.name == 'posix' and BASH, 'requires a POSIX HOST with Bash')
class SetupStatusTests(unittest.TestCase):
    def invoke(self, *, case='return', status=0, mode=''):
        text = SOURCE.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, events = root / 'fixture.sh', root / 'events'
            script.write_text(DOUBLES + '\n' + declaration(text, 'run_with_timeout')
                              + '\n' + declaration(text, 'main')
                              + '\nmain "$FIXTURE_MODE"\nstatus=$?\nexit "$status"\n',
                              encoding='utf-8')
            environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': str(root / 'guest-home'),
                           'EVENTS': str(events), 'FIXTURE_CASE': case,
                           'FIXTURE_SETUP_STATUS': str(status), 'FIXTURE_MODE': mode}
            process = subprocess.run([BASH, '--noprofile', '--norc', str(script)],
                                     env=environment, stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=3)
            return process, events.read_text().splitlines()

    def test_nonzero_setup_status_stops_later_provisioning(self):
        process, events = self.invoke(status=37)
        self.assertEqual(process.returncode, 37, process.stderr.decode())
        self.assertEqual([event for event in events if ':' not in event],
                         ['password', 'autologin', 'setup'])
        self.assertNotIn('info:Configuration complete!', events)

    def test_actual_timeout_preserves_failure_and_blocks_later_steps(self):
        process, events = self.invoke(case='timeout')
        self.assertEqual(process.returncode, 137, process.stderr.decode())
        self.assertEqual([event for event in events if ':' not in event],
                         ['password', 'autologin', 'setup'])
        self.assertNotIn('info:Configuration complete!', events)

    def test_success_runs_existing_remaining_steps_in_order(self):
        process, events = self.invoke()
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        self.assertEqual([event for event in events if ':' not in event],
                         ['password', 'autologin', 'setup', 'screensaver',
                          'ssh-keys', 'chezmoi'])
        self.assertEqual(events[-1], 'info:Configuration complete!')

    def test_runner_subcommand_does_not_run_setup(self):
        process, events = self.invoke(status=37, mode='runner')
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        self.assertIn('runner', events)
        self.assertNotIn('setup', events)
        self.assertNotIn('screensaver', events)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *unittest_args])
