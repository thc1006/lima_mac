"""Focused real collector/production-shell fixtures; no live Host or VM commands."""
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_supervisor_exit_status as lifecycle


COLLECTOR = Path(__file__).resolve().parents[1] / "preserve-failed-instance.py"
NAMES = ("ha.stderr.log", "ha.stdout.log", "serialv.log")
LIMIT = 65536
SECRET = "SYNTHETIC_ENV_OR_JIT_FILE_MUST_NOT_BE_READ"


class NoEnvironmentReads(dict):
    def __getitem__(self, key):
        raise AssertionError("unexpected environment value read: " + str(key))

    def get(self, key, default=None):
        return self[key]

    def __iter__(self):
        raise AssertionError("unexpected environment dump")

    def items(self):
        raise AssertionError("unexpected environment dump")

    def keys(self):
        raise AssertionError("unexpected environment dump")

    def values(self):
        raise AssertionError("unexpected environment dump")

    def __contains__(self, key):
        raise AssertionError("unexpected environment key probe: " + str(key))

    def copy(self):
        raise AssertionError("unexpected environment dump")


@unittest.skipUnless(os.name == "posix" and hasattr(signal, "SIGALRM"), "requires POSIX fd/signal APIs")
class FailureHarvestFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="supervisor-harvest-inert-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.instance = "macos-job-20000102030405"
        self.vm = self.home / ".lima" / self.instance
        self.vm.mkdir(parents=True)
        (self.home / "ci").mkdir(mode=0o700)
        for path in (self.home / "ci/.gh_token", self.home / "ci/.kc_pw", self.home / "ci/.env",
                     self.vm / ".runner", self.vm / ".credentials", self.vm / "jit-config", self.vm / "other.log"):
            path.write_text(SECRET)
        spec = importlib.util.spec_from_file_location("owned_harvest_fixture", COLLECTOR)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)  # Functions/constants only; never its __main__ block.
        self.values = {name: (name.encode() + b"\n") * 6000 for name in NAMES}
        for name, value in self.values.items():
            (self.vm / name).write_bytes(value)

    def run_collector(self, *, fault=None, stream=None):
        module = self.module
        real_open, real_io_open, real_write = os.open, io.open, module.write
        self.opens = []
        self.alarm = Mock()
        self.install_signal = Mock()
        output = stream if stream is not None else io.StringIO()
        allowed_reads = {self.vm / name for name in NAMES}
        output_root = self.home / "ci/failed-jobs"

        def guarded_open(path, flags, *args, **kwargs):
            path = Path(path)
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            if writing:
                self.assertIn(output_root, path.parents)
                self.assertIn(path.name, (*NAMES, "BEGIN.json", "RESULT.json"))
                self.assertTrue(flags & os.O_EXCL)
            else:
                self.assertIn(path, allowed_reads)
                self.opens.append(path)
                if fault == "timeout" and path.name == "ha.stdout.log":
                    module.timed_out(signal.SIGALRM, None)
            return real_open(path, flags, *args, **kwargs)

        def guarded_stream(path, *args, **kwargs):
            if not isinstance(path, int):
                raise AssertionError("unexpected pathname stream read/write")
            return real_io_open(path, *args, **kwargs)

        def write(path, data):
            if (fault == "log_ENOSPC" and path.name == "ha.stdout.log"
                    or fault == "result_ENOSPC" and path.name == "RESULT.json"):
                real_write(path, data[:19])
                raise OSError(errno.ENOSPC, "synthetic owned-filesystem pressure")
            return real_write(path, data)

        # The home boundary is the owned temp directory. No actual HOME value,
        # credentials, environment payload or JIT file is consulted by the body.
        with patch.object(module.Path, "home", return_value=self.home), \
                patch.object(module.sys, "argv", [str(COLLECTOR), self.instance, "255", "7"]), \
                patch.object(module.os, "environ", NoEnvironmentReads(GH_TOKEN=SECRET, JIT_CONFIG=SECRET)), \
                patch.object(module.os, "open", side_effect=guarded_open), \
                patch.object(io, "open", side_effect=guarded_stream), \
                patch.object(module, "write", side_effect=write), \
                patch.object(module.signal, "signal", self.install_signal), \
                patch.object(module.signal, "alarm", self.alarm), contextlib.redirect_stdout(output):
            return module.main()

    def stage(self):
        folders = list((self.home / "ci/failed-jobs").iterdir())
        self.assertEqual(len(folders), 1)
        return folders[0]

    def result(self):
        return json.loads((self.stage() / "RESULT.json").read_bytes())

    def test_fixed_three_tails_are_bounded_private_and_do_not_read_env_or_jit_paths(self):
        self.assertEqual(self.run_collector(), 0)
        folder, record = self.stage(), self.result()
        self.assertEqual((record["runner_rc"], record["writer_rc"]), (255, 7))
        self.assertFalse(record["guest_results_captured"])
        self.assertFalse(record["atomic_snapshot"])
        self.assertEqual(set(self.opens), {self.vm / name for name in NAMES})
        self.assertEqual({p.name for p in folder.iterdir()}, {*NAMES, "BEGIN.json", "RESULT.json"})
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for row in record["logs"]:
            name = row["name"]
            self.assertEqual((folder / name).read_bytes(), self.values[name][-LIMIT:])
            self.assertEqual((self.vm / name).read_bytes(), self.values[name])
            self.assertEqual(row["bytes"], LIMIT)
            self.assertEqual(row["sha256"], hashlib.sha256(self.values[name][-LIMIT:]).hexdigest())
            self.assertFalse(row["complete_file"])
        self.assertEqual(sum(row["bytes"] for row in record["logs"]), 192 * 1024)
        for path in folder.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(SECRET.encode(), path.read_bytes())
        self.assertEqual(self.alarm.call_args_list, [unittest.mock.call(5), unittest.mock.call(0)])
        self.install_signal.assert_called_once_with(signal.SIGALRM, self.module.timed_out)

    def test_small_logs_are_observed_as_complete_files(self):
        for name in NAMES:
            (self.vm / name).write_bytes(b"small synthetic log\n")
        self.assertEqual(self.run_collector(), 0)
        self.assertTrue(all(row["complete_file"] for row in self.result()["logs"]))

    def test_broken_console_stream_does_not_prevent_private_capture(self):
        class BrokenStream:
            def write(self, _value):
                raise BrokenPipeError(errno.EPIPE, "synthetic broken log stream")

            def flush(self):
                raise BrokenPipeError(errno.EPIPE, "synthetic broken log stream")

        self.assertEqual(self.run_collector(stream=BrokenStream()), 0)
        self.assertFalse(self.result()["read_errors"])
        self.assertEqual(sum((self.stage() / name).stat().st_size for name in NAMES), 192 * 1024)

    def test_one_log_ENOSPC_preserves_prior_and_later_log_with_explicit_error(self):
        self.assertEqual(self.run_collector(fault="log_ENOSPC"), 1)
        record = self.result()
        self.assertTrue(record["read_errors"])
        self.assertEqual(record["logs"][1]["error"], "OSError")
        self.assertEqual((self.stage() / NAMES[0]).read_bytes(), self.values[NAMES[0]][-LIMIT:])
        self.assertEqual((self.stage() / NAMES[2]).read_bytes(), self.values[NAMES[2]][-LIMIT:])
        self.assertEqual((self.stage() / NAMES[1]).read_bytes(), self.values[NAMES[1]][-LIMIT:][:19])
        self.assertNotIn("sha256", record["logs"][1])

    def test_manifest_ENOSPC_keeps_partial_stage_without_a_valid_result(self):
        with self.assertRaises(OSError):
            self.run_collector(fault="result_ENOSPC")
        self.assertTrue((self.stage() / "BEGIN.json").is_file())
        with self.assertRaises(json.JSONDecodeError):
            self.result()
        self.assertTrue(all((self.stage() / name).is_file() for name in NAMES))

    def test_fixed_log_symlink_is_not_followed_into_a_secret_file(self):
        path = self.vm / NAMES[1]
        path.unlink()
        path.symlink_to(self.home / "ci/.gh_token")
        self.assertEqual(self.run_collector(), 1)
        self.assertIn("error", self.result()["logs"][1])
        self.assertFalse((self.stage() / NAMES[1]).exists())
        self.assertEqual((self.home / "ci/.gh_token").read_text(), SECRET)

    def test_missing_log_is_recorded_and_remaining_log_is_preserved(self):
        (self.vm / NAMES[1]).unlink()
        self.assertEqual(self.run_collector(), 1)
        self.assertEqual(self.result()["logs"][1]["error"], "FileNotFoundError")
        self.assertEqual((self.stage() / NAMES[2]).read_bytes(), self.values[NAMES[2]][-LIMIT:])

    def test_timeout_keeps_begin_and_first_tail_without_claiming_a_result(self):
        with self.assertRaises(TimeoutError):
            self.run_collector(fault="timeout")
        self.assertTrue((self.stage() / "BEGIN.json").is_file())
        self.assertTrue((self.stage() / NAMES[0]).is_file())
        self.assertFalse((self.stage() / "RESULT.json").exists())
        self.assertEqual(self.alarm.call_args_list, [unittest.mock.call(5)])


@unittest.skipUnless(os.name == "posix" and lifecycle.BASH, "requires a POSIX Bash host")
class FailureHarvestLifecycleTests(unittest.TestCase):
    def test_production_hook_keeps_original_rc_and_always_cleans_up_once(self):
        harness = lifecycle.SupervisorExitStatusTests()
        for runner, writer, harvest, stop, delete, expected in (
                (255, 0, 0, 0, 0, 255), (255, 7, 124, 5, 6, 255), (0, 7, 1, 5, 6, 7)):
            with self.subTest(runner=runner, writer=writer, harvest=harvest):
                rc, output, events = harness.invoke(runner=runner, writer=writer, harvest=harvest,
                                                    stop=stop, delete=delete, record_harvest=True)
                self.assertEqual(rc, expected, output)
                mark = "harvest:%s:%s" % (runner, writer)
                self.assertEqual(events.count(mark), 1)
                self.assertEqual(events.count("stop"), 1)
                self.assertEqual(events.count("delete"), 1)
                self.assertLess(events.index(mark), events.index("stop"))
                self.assertEqual(events[-2:], ["stop", "delete"])
                if harvest:
                    self.assertIn("capture FAILED", output)

    def test_success_does_not_harvest_and_existing_cleanup_failure_priority_remains(self):
        harness = lifecycle.SupervisorExitStatusTests()
        rc, output, events = harness.invoke(stop=5, delete=6, harvest=99, record_harvest=True)
        self.assertEqual(rc, 5, output)
        self.assertFalse(any(event.startswith("harvest:") for event in events))
        self.assertEqual(events[-2:], ["stop", "delete"])


if __name__ == "__main__":
    unittest.main()
