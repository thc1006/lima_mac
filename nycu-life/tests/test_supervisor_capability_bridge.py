"""Run real Bash queue/JIT functions with real jq; all external actions are fixtures."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(os.environ.get('SUPERVISOR_UNDER_TEST',
                            str(Path(__file__).resolve().parents[1] / 'mac-ci-supervisor.sh')))
BASELINE = Path(os.environ['BASELINE_SUPERVISOR']) if 'BASELINE_SUPERVISOR' in os.environ else None
JQ = os.environ.get('FIXTURE_JQ', shutil.which('jq'))
GENERIC = 'nycu-life-macos'
EXTRA = 'nycu-life-macos-xcode-27'
NEW = 'macos-ci-xcode27-20261027'


class CapabilityBridgeTests(unittest.TestCase):
    def run_controller(self, labels, *, source=SOURCE, extra='', golden='macos-ci',
                       label=GENERIC, status='queued', drain=False):
        self.assertTrue(JQ and Path(JQ).is_file(), 'Pinned real jq is required; no skip')
        with tempfile.TemporaryDirectory(prefix='bridge-fixture-') as name:
            d = Path(name); tools = d / 'tools'; tools.mkdir()
            (d / 'jobs.json').write_text(json.dumps({'jobs': [
                {'name': 'Native iOS', 'status': status, 'labels': labels}]}))
            (d / 'token').write_text('generated-fixture-token')
            if drain:
                (d / 'drain').touch()
            commands = {
                'gh': '''#!/usr/bin/python3
import os,json,subprocess,sys
from pathlib import Path
d=Path(os.environ['FIXTURE']); a=sys.argv[1:]
with (d/'events').open('a') as f: f.write(json.dumps(['gh',*a])+'\\n')
if any('generate-jitconfig' in x for x in a):
 print('generated-fixture-jit')
elif any('/jobs' in x for x in a):
 q=a[a.index('--jq')+1]
 p=subprocess.run([os.environ['FIXTURE_JQ'],q,str(d/'jobs.json')],capture_output=True)
 sys.stdout.buffer.write(p.stdout);sys.stderr.buffer.write(p.stderr);raise SystemExit(p.returncode)
elif any('actions/runs?' in x for x in a): print('101')
else: raise SystemExit(2)
''',
                'limactl': '''#!/usr/bin/python3
import os,json,sys
from pathlib import Path
with (Path(os.environ['FIXTURE'])/'events').open('a') as f:
 f.write(json.dumps(['limactl',*sys.argv[1:]])+'\\n')
''',
                'df': "#!/bin/bash\nprintf 'Filesystem 1G-blocks Used Available Capacity iused ifree %%iused Mounted on\\nfixture 1000 1 999 1%% 1 999 1%% /\\n'\n",
                'date': "#!/bin/bash\ncase \"$*\" in *Y%m%d*) echo 20261108000000;; *) echo 00:00:00;; esac\n",
            }
            for command, body in commands.items():
                p = tools / command; p.write_text(body); p.chmod(0o700)
            raw = source.read_text()
            start = raw.index('log "supervisor starting (golden=')
            script = raw[:start]
            original = 'export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
            self.assertEqual(script.count(original), 1)
            script = script.replace(original, f'export PATH="{tools}:/usr/bin:/bin"')
            script += '\nif queued_macos_job; then provision_and_run; else exit 9; fi\n'
            p = d / 'subject.sh'; p.write_text(script)
            env = dict(os.environ, FIXTURE=str(d), FIXTURE_JQ=str(JQ),
                       GOLDEN=golden, LABEL=label, ADDITIONAL_LABEL=extra,
                       TOKEN_FILE=str(d / 'token'), KC_PW_FILE=str(d / 'absent'),
                       DRAIN_FILE=str(d / 'drain'))
            result = subprocess.run(['/bin/bash', str(p)], env=env, capture_output=True,
                                    text=True, timeout=10)
            events = [json.loads(x) for x in (d / 'events').read_text().splitlines()] \
                if (d / 'events').exists() else []
            return result, events

    @staticmethod
    def jit(events):
        return [x for x in events if x[0] == 'gh' and any('generate-jitconfig' in y for y in x)]

    def test_dual_queue_runs_new_capability_and_mints_explicit_required_labels(self):
        result, events = self.run_controller(['self-hosted', EXTRA], extra=EXTRA, golden=NEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        jit = self.jit(events)
        self.assertEqual(len(jit), 1)
        self.assertEqual([x for x in jit[0] if x.startswith('labels[]=')],
                         ['labels[]=self-hosted', 'labels[]=macOS', 'labels[]=ARM64',
                          'labels[]=' + GENERIC, 'labels[]=' + EXTRA])
        self.assertTrue({'self-hosted', EXTRA} <=
                        {x.removeprefix('labels[]=') for x in jit[0] if x.startswith('labels[]=')})
        self.assertEqual(sum(x[:2] == ['limactl', 'clone'] for x in events), 1)
        self.assertEqual(sum(x[:2] == ['limactl', 'shell'] for x in events), 1)
        self.assertEqual(sum(x[:2] == ['limactl', 'delete'] for x in events), 1)

    def test_dual_queue_still_runs_legacy_jobs(self):
        result, events = self.run_controller([GENERIC], extra=EXTRA, golden=NEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.jit(events)), 1)

    def test_legacy_default_never_advertises_or_selects_new_capability(self):
        result, events = self.run_controller([EXTRA])
        self.assertEqual(result.returncode, 9)
        self.assertEqual(self.jit(events), [])
        result, events = self.run_controller([GENERIC])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([x for x in self.jit(events)[0] if x.startswith('labels[]=')],
                         ['labels[]=' + GENERIC])

    def test_default_legacy_external_argv_and_lifecycle_equal_installed_a31(self):
        self.assertTrue(BASELINE and BASELINE.is_file(), 'Pinned installed a31 required')
        before, a = self.run_controller([GENERIC], source=BASELINE)
        after, b = self.run_controller([GENERIC])
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertEqual(after.returncode, before.returncode, after.stderr)
        self.assertEqual(a, b)

    def test_unsupported_required_labels_or_nonqueued_jobs_cannot_mint(self):
        for labels, status in [([EXTRA, 'linux'], 'queued'),
                               ([EXTRA, 'unqualified-image'], 'queued'),
                               (['self-hosted'], 'queued'), ([], 'queued'),
                               ([EXTRA], 'completed'), ([GENERIC], 'in_progress')]:
            with self.subTest(labels=labels, status=status):
                result, events = self.run_controller(labels, status=status, extra=EXTRA, golden=NEW)
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertEqual(self.jit(events), [])

    def test_unknown_extra_or_wrong_image_or_legacy_binding_refuses_before_api(self):
        for extra, golden, label in [('other', NEW, GENERIC), ('self-hosted', NEW, GENERIC),
                                     (EXTRA, 'macos-ci', GENERIC),
                                     (EXTRA, NEW, 'other'), (EXTRA + ',' + GENERIC, NEW, GENERIC)]:
            with self.subTest(extra=extra, golden=golden, label=label):
                result, events = self.run_controller([EXTRA], extra=extra, golden=golden, label=label)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(events, [])

    def test_owned_or_foreign_drain_still_prevents_clone_and_jit(self):
        result, events = self.run_controller([EXTRA], extra=EXTRA, golden=NEW, drain=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(x[0] == 'limactl' for x in events))
        self.assertEqual(self.jit(events), [])


if __name__ == '__main__':
    unittest.main()
