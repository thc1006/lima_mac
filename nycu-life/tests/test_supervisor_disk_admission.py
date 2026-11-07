#!/usr/bin/env python3
"""Exercise production disk admission with offline df/awk/lifecycle doubles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_supervisor_exit_status import BASH, SOURCE, extract_function, unittest_args

HEADER = 'Filesystem 1G-blocks Used Available Capacity iused ifree %iused Mounted on\n'


def report(available):
    return HEADER + '/dev/disk3s1s1 228 11 ' + str(available) + ' 33% 458732 258900040 0% /\n'


@unittest.skipUnless(os.name == 'posix' and BASH, 'requires a POSIX Bash host')
class SupervisorDiskAdmissionTests(unittest.TestCase):
    def invoke(self, output, *, df_rc=0, awk_rc=0, threshold='15', mode='probe'):
        text = SOURCE.read_text()
        with tempfile.TemporaryDirectory(prefix='sa-supervisor-disk-') as folder:
            directory = Path(folder)
            fixture = directory / 'df'
            fixture.write_text(output)
            events = directory / 'events'
            code = '''set -u
GOLDEN=fixture-golden
POLL=.01
log() { printf '%s\\n' "$*"; }
df() {
  printf 'df\\n' >> "$EVENTS"
  [ "$*" = '-g /' ] || return 66
  cat "$DF_FIXTURE"
  return "$DF_RC"
}
awk() {
  [ "$AWK_RC" = 0 ] || return "$AWK_RC"
  command awk "$@"
}
draining() { return 1; }
human_active() { return 1; }
unlock_keychain() { printf 'unlock\\n' >> "$EVENTS"; }
gh_api() { printf 'jit\\n' >> "$EVENTS"; return 1; }
limactl() { printf '%s\\n' "$1" >> "$EVENTS"; return 9; }
queued_macos_job() { printf 'queued\\n' >> "$EVENTS"; DF_RC=7; return 0; }
sleep() { printf 'sleep\\n' >> "$EVENTS"; exit 99; }
'''
            code += extract_function(text, 'host_unhealthy')
            if mode != 'probe':
                code += extract_function(text, 'provision_and_run')
            if mode == 'loop':
                marker = '\nwhile true; do\n'
                self.assertEqual(text.count(marker), 1)
                code += text[text.index(marker):]
            else:
                code += ('host_unhealthy\n' if mode == 'probe' else 'provision_and_run\n')
            script = directory / 'fixture.sh'
            script.write_text(code)
            env = dict(os.environ, MIN_FREE_GB=threshold, DF_FIXTURE=str(fixture),
                       EVENTS=str(events), DF_RC=str(df_rc), AWK_RC=str(awk_rc))
            process = subprocess.run([BASH, str(script)], capture_output=True, text=True,
                                     env=env, timeout=5)
            return process, events.read_text().splitlines() if events.exists() else []

    def test_low_space_pauses(self):
        for available in (0, 1, 14):
            with self.subTest(available=available):
                process, _ = self.invoke(report(available))
                self.assertEqual(process.returncode, 0)

    def test_equal_or_greater_space_admits(self):
        for available in (15, 24, 99, 999999999):
            with self.subTest(available=available):
                process, _ = self.invoke(report(available))
                self.assertEqual(process.returncode, 1, process.stdout + process.stderr)

    def test_failed_df_rejects_even_plausible_output(self):
        for status in (1, 7, 127):
            with self.subTest(status=status):
                process, _ = self.invoke(report(99), df_rc=status)
                self.assertEqual(process.returncode, 0)
                self.assertIn('disk probe FAILED', process.stdout)

    def test_malformed_or_ambiguous_output_pauses(self):
        cases = ('', HEADER, report(''), report('-1'), report('1.5'), report('garbage'),
                 report(9999999999), report('9223372036854775808'),
                 report(99) + report(99).split('\n', 1)[1],
                 report(99).replace('1G-blocks', '512-blocks'),
                 report(99).replace('Available', 'Unknown'),
                 report(99).replace('0% /', '0% /Volumes/other'))
        for output in cases:
            with self.subTest(output=output):
                process, _ = self.invoke(output)
                self.assertEqual(process.returncode, 0)
                self.assertIn('disk probe invalid', process.stdout)

    def test_failed_parser_pauses(self):
        process, _ = self.invoke(report(99), awk_rc=127)
        self.assertEqual(process.returncode, 0)
        self.assertIn('disk probe invalid', process.stdout)

    def test_invalid_threshold_pauses_before_df(self):
        for threshold in ('', '-1', '1.5', 'abc', '15\n99', '9223372036854775808'):
            with self.subTest(threshold=threshold):
                process, events = self.invoke(report(99), threshold=threshold)
                self.assertEqual(process.returncode, 0)
                self.assertIn('invalid disk threshold', process.stdout)
                self.assertEqual(events, [])

    def test_configured_threshold_is_preserved(self):
        for threshold, result in (('0', 1), ('24', 1), ('25', 0)):
            with self.subTest(threshold=threshold):
                process, _ = self.invoke(report(24), threshold=threshold)
                self.assertEqual(process.returncode, result)

    def test_unknown_disk_prevents_lifecycle_side_effects(self):
        process, events = self.invoke(report(99), df_rc=1, mode='provision')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(events, ['df'])
        self.assertIn('skip queued job', process.stdout)

    def test_healthy_disk_reaches_existing_clone_path(self):
        process, events = self.invoke(report(24), mode='provision')
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(events, ['df', 'clone'])

    def test_unknown_disk_prevents_queue_lookup(self):
        process, events = self.invoke(report(99), df_rc=1, mode='loop')
        self.assertEqual(process.returncode, 99, process.stderr)
        self.assertEqual(events, ['df', 'sleep'])

    def test_disk_failure_during_queue_lookup_prevents_clone(self):
        process, events = self.invoke(report(24), mode='loop')
        self.assertEqual(process.returncode, 99, process.stderr)
        self.assertEqual(events, ['df', 'queued', 'df', 'df', 'sleep'])
        self.assertIn('skip queued job', process.stdout)


if __name__ == '__main__':
    unittest.main(argv=[__file__, *unittest_args])
