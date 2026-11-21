"""Inert actual-shell regressions; run only in the approved remote AMD worker."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "mac-ci-supervisor.sh"
SECRET = "FIXTURE_ONLY_MUST_NOT_REACH_LOGS_7b6e"


def encoded_configuration(configuration=None):
    if configuration is None:
        configuration = {".runner": base64.b64encode(b'{"fixture":true}').decode(),
                         ".credentials": ""}
    return base64.b64encode(json.dumps(configuration, separators=(",", ":")).encode()).decode()


class SupervisorJITTests(unittest.TestCase):
    def invoke(self, response, api_rc=0, *, main=False, initial_drain=False,
               drain_during_runner=False, runner_rc=0, writer_rc=0, stop_rc=0, delete_rc=0):
        source = SOURCE.read_text()
        boundary = '\nlog "supervisor starting (golden='
        self.assertEqual(source.count(boundary), 1)
        split = source.index(boundary)
        with tempfile.TemporaryDirectory(prefix="supervisor-jit-inert-") as folder:
            root = Path(folder)
            prefix = root / "supervisor-functions.sh"
            prefix.write_text(source[:split])
            driver = root / "driver.sh"
            driver.write_text(r'''#!/bin/bash
source "$TEST_PREFIX"
log() { printf '%s\n' "$*"; }
human_active() { return 1; }
host_unhealthy() { return 1; }
unlock_keychain() { printf 'unlock\n' >> "$TEST_EVENTS"; return 0; }
reconcile() { printf 'reconcile\n' >> "$TEST_EVENTS"; }
queued_macos_job() {
  printf 'queue\n' >> "$TEST_EVENTS"
  if [ ! -e "$TEST_QUEUED_ONCE" ]; then : > "$TEST_QUEUED_ONCE"; return 0; fi
  return 1
}
gh_api() {
  printf 'api\n' >> "$TEST_EVENTS"
  printf '%s' "$TEST_RESPONSE"
  printf '%s' "$TEST_SECRET" >&2
  return "$TEST_API_RC"
}
limactl() {
  printf '%s\n' "$1" >> "$TEST_EVENTS"
  case "$1" in
    clone|start) return 0 ;;
    shell)
      [ "$6" = "cd ~/actions-runner && ./run.sh --jitconfig '$TEST_EXPECTED_JIT'" ] || return 91
      if [ "$TEST_DRAIN_DURING_RUNNER" = 1 ]; then printf 'owned-test-marker\n' > "$DRAIN_FILE"; fi
      return "$TEST_RUNNER_RC" ;;
    stop) return "$TEST_STOP_RC" ;;
    delete) return "$TEST_DELETE_RC" ;;
    *) return 92 ;;
  esac
}
sed() { /bin/cat; return "$TEST_WRITER_RC"; }
sleep() { printf 'sleep:%s\n' "$1" >> "$TEST_EVENTS"; exit 0; }
if [ "$TEST_MAIN" = 1 ]; then
  source "$TEST_MAIN_TAIL"
else
  provision_and_run
  exit "$?"
fi
''')
            tail = root / "main-tail.sh"
            tail.write_text(source[split:])
            events = root / "events"
            events.write_text("")
            drain = root / "maintenance.drain"
            if initial_drain:
                drain.write_text("existing-test-marker\n")
            value = response.get("encoded_jit_config", "") if isinstance(response, dict) else ""
            env = {"HOME": folder, "PATH": "/usr/bin:/bin", "LC_ALL": "C", "POLL": "15",
                   "DRAIN_FILE": str(drain), "TEST_PREFIX": str(prefix), "TEST_MAIN_TAIL": str(tail),
                   "TEST_EVENTS": str(events), "TEST_QUEUED_ONCE": str(root / "queued-once"),
                   "TEST_RESPONSE": response if isinstance(response, str) else json.dumps(response),
                   "TEST_EXPECTED_JIT": value if isinstance(value, str) else "",
                   "TEST_SECRET": SECRET, "TEST_API_RC": str(api_rc), "TEST_MAIN": str(int(main)),
                   "TEST_DRAIN_DURING_RUNNER": str(int(drain_during_runner)),
                   "TEST_RUNNER_RC": str(runner_rc), "TEST_WRITER_RC": str(writer_rc),
                   "TEST_STOP_RC": str(stop_rc), "TEST_DELETE_RC": str(delete_rc)}
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", str(driver)],
                                    env=env, capture_output=True, timeout=8)
            calls = events.read_text().splitlines()
            output = (result.stdout + result.stderr).decode()
            self.assertNotIn(SECRET, output)
            if isinstance(value, str) and value:
                self.assertNotIn(value, output)
            self.assertEqual(calls.count("api"), 0 if initial_drain else 1)
            return result.returncode, calls, output

    def test_valid_canonical_config_and_empty_inner_file_run_once_and_cleanup(self):
        rc, calls, _ = self.invoke({"encoded_jit_config": encoded_configuration()})
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["clone", "unlock", "start", "api", "shell", "stop", "delete"])

    def test_nonzero_api_status_rejects_even_valid_response_without_post_replay(self):
        for status in (1, 4, 28):
            with self.subTest(status=status):
                rc, calls, output = self.invoke({"encoded_jit_config": encoded_configuration()}, status)
                self.assertEqual(rc, 1)
                self.assertNotIn("shell", calls)
                self.assertEqual(calls[-2:], ["stop", "delete"])
                self.assertIn("jitconfig failed", output)

    def test_invalid_response_types_missing_fields_and_json_never_run(self):
        for response in ("not-json", "null", "[]", "{}", {"message": "Not Found"},
                         {"encoded_jit_config": None}, {"encoded_jit_config": ""},
                         {"encoded_jit_config": 1}, {"encoded_jit_config": True},
                         {"encoded_jit_config": []}, {"encoded_jit_config": {}}):
            with self.subTest(response=response):
                rc, calls, _ = self.invoke(response)
                self.assertEqual(rc, 1)
                self.assertNotIn("shell", calls)
                self.assertEqual(calls[-2:], ["stop", "delete"])

    def test_literal_null_and_malformed_or_noncanonical_base64_never_run(self):
        good = encoded_configuration()
        padded = encoded_configuration({".runner": ""})
        self.assertTrue(padded.endswith("="))
        decoded_list = base64.b64encode(b'[]').decode()
        decoded_empty = base64.b64encode(b'{}').decode()
        # AB== decodes but contains nonzero unused padding bits; AA== is canonical.
        for value in ("null", "!", good + "\n", padded.rstrip("="), "AB==", "____",
                      decoded_list, decoded_empty, "'; echo " + SECRET + "; #"):
            with self.subTest(value=value):
                rc, calls, _ = self.invoke({"encoded_jit_config": value})
                self.assertEqual(rc, 1)
                self.assertNotIn("shell", calls)

    def test_decoded_runner_map_requires_string_values_and_canonical_file_base64(self):
        for mapping in ({".runner": 5}, {".runner": None}, {".runner": "!"},
                        {".runner": "AB=="}):
            with self.subTest(mapping=mapping):
                rc, calls, _ = self.invoke({"encoded_jit_config": encoded_configuration(mapping)})
                self.assertEqual(rc, 1)
                self.assertNotIn("shell", calls)

    def test_actual_main_failure_backs_off_before_next_lookup_or_post(self):
        rc, calls, _ = self.invoke({"encoded_jit_config": encoded_configuration()}, 1, main=True)
        self.assertEqual(rc, 0)  # the inert sleep terminates the one observed loop
        self.assertEqual(calls.count("queue"), 1)
        self.assertEqual(calls[-3:], ["stop", "delete", "sleep:15"])

    def test_existing_successful_main_path_and_drain_preserve_natural_cleanup(self):
        rc, calls, _ = self.invoke({"encoded_jit_config": encoded_configuration()},
                                   main=True, drain_during_runner=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls.count("queue"), 1)
        self.assertEqual(calls[-4:], ["shell", "stop", "delete", "sleep:15"])
        rc, calls, _ = self.invoke({"encoded_jit_config": encoded_configuration()},
                                   main=True, initial_drain=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["sleep:15"])

    def test_existing_runner_writer_and_cleanup_failure_priority_is_preserved(self):
        for runner, writer, stop, delete, expected in ((7, 0, 11, 12, 7),
                (0, 9, 11, 12, 9), (0, 0, 11, 12, 11), (0, 0, 0, 12, 12)):
            with self.subTest(runner=runner, writer=writer, stop=stop, delete=delete):
                rc, calls, _ = self.invoke({"encoded_jit_config": encoded_configuration()},
                    runner_rc=runner, writer_rc=writer, stop_rc=stop, delete_rc=delete)
                self.assertEqual(rc, expected)
                self.assertEqual(calls[-2:], ["stop", "delete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
