#!/usr/bin/env python3
"""Best-effort small Host evidence before the existing failed-instance deletion."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time


LIMIT = 65536
LOGS = ("ha.stderr.log", "ha.stdout.log", "serialv.log")


def write(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def timed_out(_signal, _frame):
    raise TimeoutError("failure evidence time limit")


def emit(message, stream=None):
    # A failed supervisor log stream must not prevent the private capture.
    try:
        print(message, file=stream or sys.stdout, flush=True)
    except TimeoutError:
        raise
    except OSError:
        pass


def main():
    if len(sys.argv) != 4 or not re.fullmatch(r"macos-job-[0-9]{14}", sys.argv[1]):
        raise ValueError("expected the supervisor's exact ephemeral instance name")
    instance = sys.argv[1]
    codes = [int(value) for value in sys.argv[2:]]
    if any(value < 0 or value > 255 for value in codes):
        raise ValueError("invalid lifecycle return code")
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(5)
    home = Path.home()
    parent = home / "ci/failed-jobs"
    parent.mkdir(mode=0o700, exist_ok=True)
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or parent.resolve() != parent
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("expected a private owned failure-evidence directory")
    folder = parent / (instance + "-" + str(time.time_ns()))
    folder.mkdir(mode=0o700)
    space = os.statvfs(home)
    record = {"instance": instance, "at": time.time(), "runner_rc": codes[0], "writer_rc": codes[1],
              "Host_available": space.f_bavail * space.f_frsize, "logs": [],
              "guest_results_captured": False, "atomic_snapshot": False}
    write(folder / "BEGIN.json", (json.dumps(record, sort_keys=True) + "\n").encode())
    source = home / ".lima" / instance
    info = source.lstat()
    if not stat.S_ISDIR(info.st_mode) or source.resolve() != source or info.st_uid != os.getuid():
        raise ValueError("expected the owned real instance directory")
    record["instance_identity"] = {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid}
    for name in LOGS:
        row = {"name": name}
        try:
            descriptor = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("expected a regular instance log")
                os.lseek(descriptor, max(0, before.st_size - LIMIT), os.SEEK_SET)
                data = os.read(descriptor, LIMIT)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            write(folder / name, data)
            stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                       total_bytes_before=before.st_size, unchanged_during_read=stable,
                       complete_file=stable and len(data) == before.st_size)
        except TimeoutError:
            raise
        except (OSError, ValueError) as error:
            row["error"] = type(error).__name__
        record["logs"].append(row)
    record["finished_at"] = time.time()
    record["read_errors"] = any("error" in row for row in record["logs"])
    raw = (json.dumps(record, sort_keys=True) + "\n").encode()
    write(folder / "RESULT.json", raw)
    emit("[failure-evidence] result=" + str(folder / "RESULT.json")
         + " sha256=" + hashlib.sha256(raw).hexdigest())
    signal.alarm(0)
    return 1 if record["read_errors"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TimeoutError:
        raise SystemExit(1)  # Do not start more output after the capture deadline.
    except (OSError, ValueError):
        # The supervisor logs capture failure; do not depend on its broken stream.
        raise SystemExit(1)
