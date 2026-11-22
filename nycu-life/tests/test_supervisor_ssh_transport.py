"""Observe the real provisioning function with offline Lima command doubles."""
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

import test_supervisor_exit_status as lifecycle


EXPECTED_SSH = [
    "/usr/bin/ssh",
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-o", "ControlPersist=no",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    "-o", "ConnectTimeout=10",
]


@unittest.skipUnless(os.name == "posix" and lifecycle.BASH, "requires a POSIX Bash host")
class SupervisorSSHTransportTests(unittest.TestCase):
    def run_case(self, **options):
        observations = []
        helper = lifecycle.SupervisorExitStatusTests("test_success_still_cleans_up")
        status, output, events = helper.invoke(ssh_observations=observations, **options)
        return status, output, events, observations

    def test_override_is_limited_to_the_job_shell(self):
        status, output, events, observations = self.run_case()
        self.assertEqual(status, 0, output)
        self.assertEqual([row["action"] for row in observations],
                         ["clone", "start", "shell", "stop", "delete"])
        for row in observations:
            if row["action"] == "shell":
                self.assertEqual(row["parsed_SSH"], EXPECTED_SSH)
            else:
                self.assertIsNone(row["SSH"], row)
        self.assertEqual(events[-2:], ["stop", "delete"])

    def test_literal_fixed_argv_overrides_inherited_shell_text_without_eval(self):
        with tempfile.TemporaryDirectory(prefix="supervisor-ssh-literal-") as folder:
            sentinel = Path(folder) / "must-not-exist"
            inherited = "$(touch " + shlex.quote(str(sentinel)) + ")"
            status, output, _, observations = self.run_case(inherited_ssh=inherited)
            self.assertEqual(status, 0, output)
            shell = next(row for row in observations if row["action"] == "shell")
            self.assertEqual(shell["parsed_SSH"], EXPECTED_SSH)
            for row in observations:
                if row["action"] != "shell":
                    self.assertEqual(row["SSH"], inherited, row)
            self.assertFalse(sentinel.exists())

    def test_existing_remote_runner_command_and_arguments_are_preserved(self):
        status, output, _, observations = self.run_case()
        self.assertEqual(status, 0, output)
        shell = next(row for row in observations if row["action"] == "shell")
        arguments = shell["argv"]
        self.assertEqual(arguments[0], "shell")
        self.assertRegex(arguments[1], r"^macos-job-[0-9]{14}$")
        self.assertEqual(arguments[2:5], ["--", "bash", "-lc"])
        self.assertEqual(arguments[5:], [
            "cd ~/actions-runner && ./run.sh --jitconfig 'eyIucnVubmVyIjoiZTMwPSJ9'"
        ])

    def test_transport_255_preserves_failure_harvest_and_cleanup(self):
        status, output, events, observations = self.run_case(
            runner=255, record_harvest=True)
        self.assertEqual(status, 255, output)
        self.assertRegex(output, r"\[run\] runner FAILED .*rc=255")
        self.assertEqual(events.count("shell"), 1, events)
        self.assertEqual(events.count("harvest:255:0"), 1, events)
        self.assertLess(events.index("harvest:255:0"), events.index("stop"))
        self.assertEqual(events[-2:], ["stop", "delete"])
        self.assertEqual(next(row["parsed_SSH"] for row in observations
                              if row["action"] == "shell"), EXPECTED_SSH)

    def test_transport_failure_precedes_writer_and_cleanup_errors(self):
        status, output, events, _ = self.run_case(
            runner=255, writer=7, stop=5, delete=6, record_harvest=True)
        self.assertEqual(status, 255, output)
        self.assertRegex(output, r"\[run\] log writer FAILED .*rc=7")
        self.assertRegex(output, r"\[cleanup\] stop FAILED .*rc=5")
        self.assertRegex(output, r"\[cleanup\] delete FAILED .*rc=6")
        self.assertEqual(events.count("harvest:255:7"), 1, events)
        self.assertEqual(events.count("stop"), 1, events)
        self.assertEqual(events.count("delete"), 1, events)
        self.assertEqual(events[-2:], ["stop", "delete"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *lifecycle.unittest_args])
