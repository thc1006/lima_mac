#!/usr/bin/env python3
"""Offline PTY faults with a synthetic password and a disposable security CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'unlock-keychain.py'
SECRET = 'synthetic-keychain-fixture-42'


@unittest.skipUnless(os.name == 'posix', 'requires a POSIX PTY')
class UnlockKeychainTests(unittest.TestCase):
    def run_fixture(self, mode='success', *, metadata=None, contents=None, partial=False):
        with tempfile.TemporaryDirectory(prefix='sa-keychain-fixture-') as name:
            directory = Path(name)
            password = directory / 'password'
            password.write_bytes(contents if contents is not None else (SECRET + '\n').encode())
            password.chmod(0o600)
            if metadata == 'symlink':
                password.rename(directory / 'original')
                password.symlink_to(directory / 'original')
            elif metadata == 'hardlink':
                os.link(password, directory / 'other')
            elif metadata is not None:
                password.chmod(metadata)
            state = directory / 'state.json'
            marker = directory / 'password-read'
            security = directory / 'security'
            security.write_text('#!' + sys.executable + '\n' + f'''
import json,os,pathlib,sys,termios,time
state=pathlib.Path({str(state)!r}); password=pathlib.Path({str(password)!r})
secret={SECRET!r}; mode={mode!r}
facts={{'argv':sys.argv[1:], 'secret_absent_from_argv':all(secret not in a for a in sys.argv),
       'secret_absent_from_environment':all(secret not in v for v in os.environ.values()),
       'session':os.getsid(0),'pid':os.getpid(),'echo_disabled':not termios.tcgetattr(0)[3] & (termios.ECHO|termios.ECHONL),
       'responses':0}}
def save(): state.write_text(json.dumps(facts))
save()
if mode == 'timeout': time.sleep(10); sys.exit(1)
if mode == 'early_failure': print(secret); sys.exit(9)
if mode == 'replacement':
    new=password.with_name('new'); new.write_text(secret+'\\n'); new.chmod(0o600); os.replace(new,password)
if mode == 'echo':
    attributes=termios.tcgetattr(0); attributes[3]|=termios.ECHO; termios.tcsetattr(0,termios.TCSANOW,attributes)
prompt='password to unlock '+sys.argv[2]+': '
if mode == 'wrong_keychain': prompt='password to unlock /different.keychain-db: '
if mode == 'fragmented':
    for part in (prompt[:9],prompt[9:21],prompt[21:]):
        os.write(1,part.encode()); time.sleep(.02)
else: os.write(1,prompt.encode())
reply=sys.stdin.buffer.readline()
facts['responses']=1; facts['password_matched']=reply==(secret+'\\n').encode(); save()
if mode == 'duplicate':
    os.write(1,('\\n'+prompt).encode()); sys.stdin.buffer.readline(); facts['responses']=2; save()
if mode == 'failure': print(secret); sys.exit(7)
sys.exit(0 if facts['password_matched'] else 8)
''', encoding='utf-8')
            security.chmod(0o700)
            wrapper = directory / 'invoke.py'
            wrapper.write_text(f'''
import importlib.util,pathlib,sys
spec=importlib.util.spec_from_file_location('private_unlock',{str(SOURCE)!r})
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
read=module.password_bytes
def observed_read(*args):
    pathlib.Path({str(marker)!r}).touch()
    return read(*args)
module.password_bytes=observed_read
if {partial!r}:
    write=module.os.write
    def partial_write(fd,data): return write(fd,data[:2])
    module.os.write=partial_write
unlock=module.unlock
module.unlock=lambda password,keychain: unlock(password,keychain,security={str(security)!r},timeout=.4)
sys.argv=['unlock-keychain.py',{str(password)!r},{str(directory / 'disposable.keychain-db')!r}]
raise SystemExit(module.main())
''', encoding='utf-8')
            started = time.monotonic()
            result = subprocess.run([sys.executable, str(wrapper)], capture_output=True, timeout=5)
            elapsed = time.monotonic() - started
            facts = json.loads(state.read_text()) if state.exists() else None
            if facts:
                with self.assertRaises(ProcessLookupError):
                    os.kill(facts['pid'], 0)
            self.assertNotIn(SECRET.encode(), result.stdout + result.stderr)
            if result.returncode:
                self.assertEqual(result.stdout, b'')
                self.assertEqual(result.stderr, b'keychain unlock failed\n')
            return result.returncode, facts, marker.exists(), elapsed

    def test_single_private_response_preserves_session_and_has_no_argv_or_env_secret(self):
        for mode in ('success', 'fragmented'):
            with self.subTest(mode=mode):
                rc, facts, read, _ = self.run_fixture(mode)
                self.assertEqual(rc, 0)
                self.assertTrue(read)
                self.assertEqual(facts['argv'][0], 'unlock-keychain')
                self.assertEqual(len(facts['argv']), 2)
                self.assertTrue(facts['secret_absent_from_argv'])
                self.assertTrue(facts['secret_absent_from_environment'])
                self.assertTrue(facts['echo_disabled'])
                self.assertEqual(facts['session'], os.getsid(0))
                self.assertEqual(facts['responses'], 1)
                self.assertTrue(facts['password_matched'])

    def test_private_read_only_file_and_partial_writes_work(self):
        rc, facts, _, _ = self.run_fixture(metadata=0o400, partial=True)
        self.assertEqual(rc, 0)
        self.assertTrue(facts['password_matched'])

    def test_invalid_file_metadata_stops_before_native_execution(self):
        for metadata in ('symlink', 'hardlink', 0o644):
            with self.subTest(metadata=metadata):
                rc, facts, read, _ = self.run_fixture(metadata=metadata)
                self.assertEqual(rc, 1)
                self.assertIsNone(facts)
                self.assertFalse(read)

    def test_empty_and_oversized_files_stop_before_native_execution(self):
        for contents in (b'', b'x' * 1025):
            with self.subTest(size=len(contents)):
                rc, facts, read, _ = self.run_fixture(contents=contents)
                self.assertEqual(rc, 1)
                self.assertIsNone(facts)
                self.assertFalse(read)

    def test_file_replacement_after_metadata_check_sends_no_response(self):
        rc, facts, read, _ = self.run_fixture('replacement')
        self.assertEqual(rc, 1)
        self.assertTrue(read)
        self.assertEqual(facts['responses'], 0)

    def test_terminal_control_characters_are_not_sent(self):
        for contents in (b'unsafe\x15password', b'unsafe\npassword', b'unsafe\x00password'):
            with self.subTest(contents=repr(contents)):
                rc, facts, _, _ = self.run_fixture(contents=contents)
                self.assertEqual(rc, 1)
                self.assertEqual(facts['responses'], 0)

    def test_reenabled_echo_sends_no_response(self):
        rc, facts, read, _ = self.run_fixture('echo')
        self.assertEqual(rc, 1)
        self.assertFalse(read)
        self.assertEqual(facts['responses'], 0)

    def test_prompt_for_a_different_keychain_never_reads_the_password(self):
        rc, facts, read, _ = self.run_fixture('wrong_keychain')
        self.assertEqual(rc, 1)
        self.assertFalse(read)
        self.assertEqual(facts['responses'], 0)

    def test_duplicate_prompt_never_gets_a_second_response(self):
        rc, facts, _, _ = self.run_fixture('duplicate')
        self.assertEqual(rc, 1)
        self.assertEqual(facts['responses'], 1)

    def test_timeout_is_bounded_and_does_not_read_password_without_prompt(self):
        rc, facts, read, elapsed = self.run_fixture('timeout')
        self.assertEqual(rc, 1)
        self.assertFalse(read)
        self.assertEqual(facts['responses'], 0)
        self.assertLess(elapsed, 3)

    def test_native_failure_output_is_never_forwarded(self):
        for mode in ('early_failure', 'failure'):
            with self.subTest(mode=mode):
                rc, _, _, _ = self.run_fixture(mode)
                self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
