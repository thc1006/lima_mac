#!/usr/bin/env python3
"""Give native security one private PTY response without changing audit sessions."""
import os
from pathlib import Path
import pty
import re
import select
import signal
import stat
import subprocess
import sys
import termios
import time


def private_metadata(path):
    value = path.lstat()
    if not (stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid()
            and value.st_nlink == 1 and stat.S_IMODE(value.st_mode) in (0o400, 0o600)
            and 0 < value.st_size <= 1024):
        raise ValueError('private file required')
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode,
            value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def password_bytes(path, expected):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = os.fstat(descriptor)
        current = (value.st_dev, value.st_ino, value.st_uid, value.st_mode,
                   value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if current != expected:
            raise ValueError('private file changed')
        raw = os.read(descriptor, 1025)
        if len(raw) != value.st_size or private_metadata(path) != expected:
            raise ValueError('private file changed')
        password = raw.rstrip(b'\r\n')
        # PTY line discipline interprets control characters; never send those as input.
        if not password or any(byte < 32 or byte == 127 for byte in password):
            raise ValueError('unsupported password transport')
        return password
    finally:
        os.close(descriptor)


def unlock(password_file, keychain, *, security='/usr/bin/security', timeout=15):
    expected = private_metadata(password_file)
    master, slave = pty.openpty()
    process = None
    pending = b''
    sent = False
    prompt = re.compile(rb'(?m)(?:^|[\r\n])password to unlock '
                        + re.escape(os.fsencode(keychain)) + rb':\s*$')
    deadline = time.monotonic() + timeout
    try:
        attributes = termios.tcgetattr(slave)
        attributes[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        # No setsid/start_new_session: preserve the supervisor's security audit session.
        process = subprocess.Popen([security, 'unlock-keychain', str(keychain)],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   close_fds=True)
        os.close(slave)
        slave = -1
        os.set_blocking(master, False)
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise TimeoutError('unlock timeout')
            if not select.select([master], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(master, 4096)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b''
            pending = (pending + chunk)[-8192:]
            if prompt.search(pending):
                if sent or termios.tcgetattr(master)[3] & (termios.ECHO | termios.ECHONL):
                    raise ValueError('unsafe unlock prompt')
                payload = password_bytes(password_file, expected) + b'\n'
                position = 0
                while position < len(payload):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('unlock write timeout')
                    if not select.select([], [master], [], 0.1)[1]:
                        continue
                    if termios.tcgetattr(master)[3] & (termios.ECHO | termios.ECHONL):
                        raise ValueError('echo enabled')
                    try:
                        count = os.write(master, payload[position:])
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise OSError('unlock write stopped')
                    position += count
                payload = b''
                pending = b''
                sent = True
        return process.wait(timeout=2) == 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if slave != -1:
            os.close(slave)
        os.close(master)
        pending = b''


def main():
    def interrupted(_number, _frame):
        raise InterruptedError('unlock interrupted')

    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, interrupted)
    try:
        if len(sys.argv) != 3 or not unlock(Path(sys.argv[1]), Path(sys.argv[2])):
            raise ValueError('unlock failed')
    except (Exception, KeyboardInterrupt):
        # Never forward native output, tracebacks, or a password-bearing exception.
        print('keychain unlock failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
