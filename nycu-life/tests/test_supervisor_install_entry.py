"""Offline caller checks; no real token, HOST service or GitHub request."""
import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import supervisor_install_entry as entry
from supervisor_install import coordinated_install
from test_supervisor_install import Facts

SYNTHETIC_CHILDREN = (
    "import os; os.write(1,b'x'*(5*1024*1024))",
    "import os; os.write(2,b'private_fixture_marker'*(64*1024))",
    "import time; time.sleep(10)",
    "import os,time; os.close(1); os.close(2); time.sleep(10)",
    "import os; os.write(1,b'{\"ok\":true}'); os.write(2,b'private_fixture_note')",
)


def successful_run():
    return {'id': entry.APPROVED_RUN, 'head_sha': entry.APPROVED_MAIN, 'workflow_id': 331308620,
            'event': 'push', 'head_branch': 'main', 'run_attempt': 1, 'status': 'completed', 'conclusion': 'success'}


class Metadata:
    def __init__(self):
        self.run = successful_run()
        self.main = entry.APPROVED_MAIN
        self.active = {}
        self.extra_jobs = {}
        self.calls = []

    def api(self, suffix, environment, deadline):
        self.calls.append((suffix, dict(environment)))
        if suffix == 'git/ref/heads/main':
            return {'object': {'sha': self.main}}
        if suffix == 'actions/runs/' + str(entry.APPROVED_RUN):
            return dict(self.run)
        if '/attempts/1/jobs?' in suffix:
            return {'total_count': 2, 'jobs': [{'name': name, 'conclusion': 'success'} for name in ('Native iOS', 'CI gate')]}
        if suffix.startswith('actions/runs?status='):
            status = suffix.split('status=')[1].split('&')[0]
            rows = self.active.get(status, [])
            return {'total_count': len(rows), 'workflow_runs': rows}
        if suffix.endswith('/jobs?per_page=100'):
            identifier = int(suffix.split('/')[2])
            rows = self.extra_jobs[identifier]
            return {'total_count': len(rows), 'jobs': rows}
        raise AssertionError('unexpected metadata endpoint')


class CallerTests(unittest.TestCase):
    def run_probe(self, data, token=b'fixture_pat_not_a_real_secret', configured=None):
        root = Path('/Users/thc1006')
        proof = entry.QueueProof(root)
        plist = plistlib.dumps({} if configured is None else configured)
        def reader(path, **kwargs):
            if path == root / 'ci/.gh_token':
                self.assertTrue(kwargs.get('private'))
                return token
            self.assertEqual(path, root / 'Library/LaunchAgents/com.nycu-life.mac-ci-supervisor.plist')
            return plist
        with mock.patch.object(entry, 'read_owned', side_effect=reader), mock.patch.object(proof, 'api', side_effect=data.api):
            return proof()

    def test_complete_five_status_inventory_uses_existing_native_token_source(self):
        data = Metadata()
        value = self.run_probe(data)
        self.assertTrue(value['complete'])
        self.assertEqual(value['statuses_checked'], list(entry.STATUSES))
        self.assertEqual(value['matching_Mac_jobs'], [])
        self.assertGreaterEqual(value['observed_epoch'], value['inventory_started_epoch'])
        self.assertNotIn('fixture_pat', json.dumps(value))
        self.assertTrue(all(call[1]['GH_TOKEN'] == 'fixture_pat_not_a_real_secret' for call in data.calls))
        self.assertEqual(sum(s.startswith('actions/runs?status=') for s, _ in data.calls), 5)

    def test_queued_in_progress_or_waiting_mac_job_denies_even_in_nonqueued_run(self):
        for status in ('queued', 'in_progress', 'waiting'):
            with self.subTest(status=status):
                data = Metadata()
                data.active['in_progress'] = [{'id': 7}]
                data.extra_jobs[7] = [{'id': 11, 'status': status, 'labels': ['self-hosted', 'nycu-life-macos']}]
                with self.assertRaisesRegex(RuntimeError, 'queued_or_active_Mac_job_priority'):
                    self.run_probe(data)

    def test_linux_active_job_does_not_invent_a_mac_job(self):
        data = Metadata()
        data.active['in_progress'] = [{'id': 7}]
        data.extra_jobs[7] = [{'id': 11, 'status': 'in_progress', 'labels': ['nycu-life-linux']}]
        self.assertEqual(self.run_probe(data)['active_runs_checked'], [7])

    def test_unknown_active_job_identity_status_or_labels_is_not_a_complete_idle_proof(self):
        examples = [
            {'id': 11, 'status': 'in_progress'},
            {'id': 11, 'status': 'in_progress', 'labels': []},
            {'id': 11, 'status': 'in_progress', 'labels': None},
            {'id': 11, 'status': 'in_progress', 'labels': 'nycu-life-linux'},
            {'id': 11, 'status': 'in_progress', 'labels': [42]},
            {'id': 11, 'status': 'in_progress', 'labels': ['']},
            {'id': 11, 'status': 'future-status', 'labels': ['nycu-life-linux']},
            {'id': 11, 'labels': ['nycu-life-linux']},
            {'id': 0, 'status': 'in_progress', 'labels': ['nycu-life-linux']},
            {'id': True, 'status': 'in_progress', 'labels': ['nycu-life-linux']},
            {'id': '11', 'status': 'in_progress', 'labels': ['nycu-life-linux']},
        ]
        for row in examples:
            with self.subTest(row=row):
                data = Metadata()
                data.active['in_progress'] = [{'id': 7}]
                data.extra_jobs[7] = [row]
                rejected_as_unavailable = False
                try:
                    self.run_probe(data)
                except RuntimeError as error:
                    rejected_as_unavailable = str(error) == 'active_job_inventory_unavailable'
                except Exception:
                    # An incidental parser exception is not a controlled complete-inventory rejection.
                    pass
                self.assertTrue(rejected_as_unavailable)

    def test_uncompleted_or_wrong_candidate_main_CI_denies_before_drain(self):
        for key, value in (('status', 'in_progress'), ('conclusion', 'failure'), ('head_sha', 'f' * 40),
                           ('run_attempt', 2), ('event', 'pull_request'), ('head_branch', 'feature')):
            with self.subTest(key=key):
                data = Metadata()
                data.run[key] = value
                with self.assertRaisesRegex(RuntimeError, 'approved_main_CI_success_required'):
                    self.run_probe(data)

    def test_changed_main_or_token_source_requires_review(self):
        data = Metadata()
        data.main = 'f' * 40
        with self.assertRaisesRegex(RuntimeError, 'approved_main_changed'):
            self.run_probe(data)
        with self.assertRaisesRegex(RuntimeError, 'existing_TOKEN_FILE_source_changed'):
            self.run_probe(Metadata(), configured={'EnvironmentVariables': {'TOKEN_FILE': '/other/token'}})

    def test_partial_and_overflow_inventories_never_become_complete(self):
        for value in ({'total_count': 1, 'jobs': []}, {'total_count': 101, 'jobs': []},
                      {'total_count': True, 'jobs': [{}]}, {'total_count': 1}):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'complete_bounded_inventory_required'):
                entry.QueueProof.complete_page(value, 'jobs')

    def test_native_api_is_GET_only_and_does_not_return_child_error_contents(self):
        proof = entry.QueueProof()
        with mock.patch.object(entry, 'bounded_metadata_child', return_value=(1, b'')) as run:
            with self.assertRaisesRegex(RuntimeError, '^GitHub_metadata_GET_unavailable$'):
                proof.api('git/ref/heads/main', {'GH_TOKEN': 'fixture'}, time.monotonic() + 1)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ['/opt/homebrew/bin/gh', 'api', '--method', 'GET'])
        self.assertNotIn('fixture', args)

    def test_expired_budget_does_not_start_gh_process(self):
        with mock.patch.object(entry.subprocess, 'Popen') as run:
            with self.assertRaisesRegex(RuntimeError, 'complete_queue_time_budget_exhausted'):
                entry.QueueProof().api('git/ref/heads/main', {}, time.monotonic() - 1)
        run.assert_not_called()

    def test_nonmetadata_endpoint_or_nonnumeric_run_cannot_select_a_request(self):
        with mock.patch.object(entry.subprocess, 'Popen') as run:
            for suffix in ('actions/secrets', '../../other', 'actions/runs/7/jobs?per_page=101', 'actions/runs/-1'):
                with self.subTest(suffix=suffix), self.assertRaisesRegex(RuntimeError, 'allowlisted_metadata_endpoint_required'):
                    entry.QueueProof().api(suffix, {}, time.monotonic() + 1)
        run.assert_not_called()
        data = Metadata()
        data.active['queued'] = [{'id': '../actions/secrets'}]
        with self.assertRaisesRegex(RuntimeError, 'numeric_workflow_run_identity_required'):
            self.run_probe(data)

    def test_canary_fails_after_bootout_and_restores_old_pair_with_fresh_pid(self):
        implementation = entry.canary_operations(Facts)
        facts = implementation()
        before = facts.service['service']['pid']
        value = coordinated_install(facts)
        self.assertEqual(value['return_code'], 37)
        self.assertTrue(value['recovered'])
        self.assertTrue(value['admission_restored'])
        self.assertFalse(value['installed'])
        self.assertTrue(facts.canary_injected)
        self.assertEqual(facts.files, facts.original_files)
        self.assertIsNone(facts.marker)
        self.assertNotEqual(facts.service['service']['pid'], before)
        self.assertLess(facts.events.index('bootout-stopped'), facts.events.index('restore'))
        self.assertNotIn('half-pair', facts.events)

    def test_canary_does_not_disguise_bootout_failure_as_injected_37(self):
        facts = entry.canary_operations(Facts)(failure='bootout')
        value = coordinated_install(facts)
        self.assertEqual(value['return_code'], 1)
        self.assertFalse(getattr(facts, 'canary_injected', False))
        self.assertTrue(value['recovered'])

    def test_default_plan_performs_no_native_or_auth_actions(self):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['supervisor_install_entry.py']), \
             mock.patch.object(entry, 'execute') as execute, contextlib.redirect_stdout(output):
            self.assertEqual(entry.main(), 0)
        execute.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['native_actions'])

    def test_apply_is_denied_before_native_until_post_canary_pin_review(self):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['supervisor_install_entry.py', '--apply']), \
             mock.patch.object(entry, 'execute') as execute, contextlib.redirect_stdout(output):
            self.assertEqual(entry.main(), 1)
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['reason'], 'post_canary_identity_and_apply_source_review_required')


class BoundedMetadataChildTests(unittest.TestCase):
    def child(self, code, *, timeout=1, select_failure=False):
        spawned = []
        original = entry.subprocess.Popen
        def owned(*args, **kwargs):
            process = original(*args, **kwargs)
            spawned.append(process)
            return process
        with mock.patch.object(entry.subprocess, 'Popen', side_effect=owned):
            boundary = mock.patch.object(entry.selectors.DefaultSelector, 'select', side_effect=OSError('private_fixture_marker')) \
                       if select_failure else contextlib.nullcontext()
            try:
                with boundary:
                    return entry.bounded_metadata_child([sys.executable, '-I', '-B', '-c', code], {},
                                                        deadline=time.monotonic() + timeout)
            finally:
                self.assertEqual(len(spawned), 1)
                for process in spawned:
                    self.assertIsNotNone(process.poll(), 'owned metadata child must be reaped')
                    self.assertTrue(process.stdout.closed)
                    self.assertTrue(process.stderr.closed)

    def test_real_stdout_overflow_is_rejected_in_flight_and_child_reaped(self):
        with self.assertRaisesRegex(RuntimeError, '^bounded_metadata_stdout_limit$'):
            self.child(SYNTHETIC_CHILDREN[0])

    def test_real_stderr_overflow_is_private_and_child_reaped(self):
        with self.assertRaisesRegex(RuntimeError, '^bounded_metadata_stderr_limit$') as error:
            self.child(SYNTHETIC_CHILDREN[1])
        self.assertNotIn('private_fixture_marker', str(error.exception))

    def test_real_command_deadline_reaps_child(self):
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, '^bounded_metadata_command_deadline$'):
            self.child(SYNTHETIC_CHILDREN[2], timeout=0.1)
        self.assertLess(time.monotonic() - started, 2)

    def test_closed_pipes_do_not_disguise_a_still_running_child(self):
        with self.assertRaisesRegex(RuntimeError, '^bounded_metadata_child_unavailable$'):
            self.child(SYNTHETIC_CHILDREN[3], timeout=0.1)

    def test_reader_exception_reaps_child_without_public_private_details(self):
        with self.assertRaisesRegex(RuntimeError, '^bounded_metadata_child_unavailable$') as error:
            self.child(SYNTHETIC_CHILDREN[2], select_failure=True)
        self.assertNotIn('private_fixture_marker', str(error.exception))

    def test_success_returns_only_stdout_and_the_owned_child_is_reaped(self):
        return_code, output = self.child(SYNTHETIC_CHILDREN[4])
        self.assertEqual(return_code, 0)
        self.assertEqual(output, b'{"ok":true}')


if __name__ == '__main__':
    unittest.main()
