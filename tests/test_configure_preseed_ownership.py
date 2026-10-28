"""HOST-only Bash fixtures for the exact production preseed heredoc.

Never source configure.sh: its other steps read credentials and change settings.
Only the declaration-to-SCRIPT block runs, with shell command doubles. The
rendered boot script also runs with doubles, including absolute macOS commands.
No Python or test helper is installed in a guest.
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


def preseed_block(text):
    starts = list(re.finditer(r'^    local SA_PRESEED_SCRIPT=', text, re.MULTILINE))
    if len(starts) != 1:
        raise AssertionError('exact production preseed block required')
    start = starts[0].start()
    end = re.search(r'^SCRIPT$', text[start:], re.MULTILINE)
    if end is None:
        raise AssertionError('production preseed heredoc terminator missing')
    return text[start:start + end.end()] + '\n'


DOUBLES = r'''set -u
record() { printf '%s\0' "$@" >> "$EVENTS"; printf '\n' >> "$EVENTS"; }
fixture_id() {
    record id "$PHASE" "$@"
    if [ "$#" -gt 1 ]; then
        [ "$LEGACY_ACCOUNT_EXISTS" = 1 ] || return 1
        case "$1" in -u) printf '%s\n' 666;; -g) printf '%s\n' 777;; *) return 91;; esac
        return 0
    fi
    if [ "$PHASE" = boot ]; then printf '%s\n' 0; return 0; fi
    case "$1" in
        -u) printf '%s\n' "$CALLER_UID"; return "$UID_STATUS";;
        -g) printf '%s\n' "$CALLER_GID"; return "$GID_STATUS";;
        *) return 92;;
    esac
}
/usr/bin/id() { fixture_id "$@"; }
id() { fixture_id "$@"; }
/usr/bin/defaults() { record defaults "$@"; }
/usr/libexec/PlistBuddy() { record PlistBuddy "$@"; }
chown() { record chown "$@"; }
chmod() { record chmod "$@"; }
sw_vers() {
    case "$1" in -buildVersion) printf '%s\n' fixture-build;;
        -productVersion) printf '%s\n' "$BOOT_VERSION";; *) return 93;; esac
}
sudo() {
    [ "$#" = 2 ] && [ "$1" = tee ] && [ "$2" = /usr/local/sbin/lima-sa-preseed.sh ] || return 94
    record write-script "$2"
    cat > "$RENDERED"
}
# Assert the doubles exist as shell functions before any production fragment is
# evaluated. The absolute native tools below are never executed on the HOST.
for tool in /usr/bin/id /usr/bin/defaults /usr/libexec/PlistBuddy; do
    [ "$(type -t "$tool")" = function ] || exit 95
done
'''


@unittest.skipUnless(os.name == 'posix' and BASH, 'requires a POSIX HOST with Bash')
class PreseedOwnershipTests(unittest.TestCase):
    def invoke(self, directory, *, version='27.1', uid='703', gid='204',
               uid_status=0, gid_status=0, spaces=False, boot=True,
               legacy_account=False, user_plist=True):
        root = Path(directory)
        guest = root / ('non blake guest home' if spaces else 'non-blake-guest')
        preferences = guest / 'Library/Preferences'
        preferences.mkdir(parents=True)
        sa = preferences / 'com.apple.SetupAssistant.plist'
        global_preferences = preferences / '.GlobalPreferences.plist'
        if user_plist:
            sa.touch()
        global_preferences.touch()
        root_home = root / 'root-boot-home'
        root_home.mkdir()
        script = root / 'fixture.sh'
        script.write_text(DOUBLES + '\nrender_preseed() {\n' + preseed_block(SOURCE.read_text(encoding='utf-8'))
                          + '}\nPHASE=render\n'
                          # Using an if context disables incidental errexit, so
                          # the production id guards must reject failure themselves.
                          + 'if render_preseed; then rc=0; else rc=$?; fi\n'
                          + 'printf "RENDER_RC=%s\\n" "$rc"\n'
                          + '[ "$rc" = 0 ] || exit "$rc"\n'
                          + 'if [ "$RUN_BOOT" = 1 ]; then PHASE=boot; HOME="$ROOT_HOME"; . "$RENDERED"; fi\n',
                          encoding='utf-8')
        events = root / 'events'
        rendered = root / 'rendered-preseed.sh'
        environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': str(guest),
            'ROOT_HOME': str(root_home), 'EVENTS': str(events), 'RENDERED': str(rendered),
            'CALLER_UID': uid, 'CALLER_GID': gid, 'UID_STATUS': str(uid_status),
            'GID_STATUS': str(gid_status), 'BOOT_VERSION': version, 'RUN_BOOT': '1' if boot else '0',
            'LEGACY_ACCOUNT_EXISTS': '1' if legacy_account else '0'}
        process = subprocess.run([BASH, '--noprofile', '--norc', str(script)], env=environment,
                                 stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        parsed = [tuple(part.decode('utf-8') for part in line.split(b'\0')[:-1])
                  for line in events.read_bytes().splitlines()] if events.exists() else []
        return process, parsed, rendered, sa, global_preferences

    def test_non_blake_caller_owns_the_root_boot_outputs_on_macos26_and27(self):
        for version in ('26.7.2', '27.1'):
            for uid, gid in (('703', '204'), ('802', '33')):
                with self.subTest(version=version, owner=uid), tempfile.TemporaryDirectory() as directory:
                    process, events, _, sa, global_preferences = self.invoke(directory, version=version, uid=uid, gid=gid)
                    self.assertEqual(process.returncode, 0, process.stderr.decode())
                    expected = [('chown', uid + ':' + gid, str(sa))]
                    if version.startswith('27.'):
                        expected.insert(0, ('chown', uid + ':' + gid, str(global_preferences)))
                    self.assertEqual([event for event in events if event[0] == 'chown'], expected)

    def test_rendered_paths_keep_guest_home_with_spaces_when_boot_home_is_root(self):
        with tempfile.TemporaryDirectory() as directory:
            process, events, rendered, sa, global_preferences = self.invoke(directory, spaces=True)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            self.assertTrue(rendered.exists())
            self.assertIn(('PlistBuddy', '-c', 'Set :AppleLanguagesSchemaVersion 5400', str(global_preferences)), events)
            self.assertIn(('chown', '703:204', str(global_preferences)), events)
            self.assertIn(('chown', '703:204', str(sa)), events)
            self.assertTrue(all(len(event) == 3 for event in events if event[0] == 'chown'))
            self.assertTrue(all(event[2] == str(sa) or event[2] == '/Library/Preferences/com.apple.SetupAssistant.managed.plist'
                                for event in events if event[0] == 'defaults'))

    def test_root_boot_does_not_requery_root_or_a_different_existing_blake_account(self):
        with tempfile.TemporaryDirectory() as directory:
            process, events, _, sa, global_preferences = self.invoke(directory, legacy_account=True)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            self.assertEqual([event for event in events if event[0] == 'id'],
                             [('id', 'render', '-u'), ('id', 'render', '-g')])
            self.assertEqual([event for event in events if event[0] == 'chown'],
                             [('chown', '703:204', str(global_preferences)), ('chown', '703:204', str(sa))])

    def test_failed_or_non_numeric_id_never_writes_the_root_script(self):
        for overrides in ({'uid_status': 1}, {'gid_status': 1}, {'uid': ''}, {'uid': '703 extra'},
                          {'gid': 'bad'}, {'uid': '703', 'uid_status': 3}):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                process, events, rendered, _, _ = self.invoke(directory, boot=False, **overrides)
                self.assertNotEqual(process.returncode, 0)
                self.assertFalse(rendered.exists())
                self.assertFalse(any(event[0] == 'write-script' for event in events))

    def test_generated_heredoc_is_valid_stock_posix_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            process, _, rendered, _, _ = self.invoke(directory, spaces=True, boot=False)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            self.assertEqual(rendered.read_bytes().splitlines()[0], b'#!/bin/sh')
            parsed = subprocess.run(['/bin/sh', '-n', str(rendered)], stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=5, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            self.assertEqual(parsed.returncode, 0, parsed.stderr.decode())

    def test_macos27_global_preseed_still_precedes_a_missing_user_sa_file(self):
        with tempfile.TemporaryDirectory() as directory:
            process, events, _, _, global_preferences = self.invoke(directory, user_plist=False)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            self.assertEqual([event for event in events if event[0] == 'chown'],
                             [('chown', '703:204', str(global_preferences))])
            self.assertFalse(any(event[0] == 'defaults' for event in events))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *unittest_args])
