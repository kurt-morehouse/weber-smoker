"""One process at a time drives the relays.

main.py, preflight.py and ssr_test.py all take this lock (an flock on
paths.gpio_lock_file) before touching a relay pin, so you can't run the relay
test while the controller holds the relays, or start two controllers. The
kernel drops the lock if the holder dies, so a crash never leaves it stuck.
"""
import fcntl
import os


class LockHeld(Exception):
    """Another process already holds the GPIO lock."""


class GpioLock:
    def __init__(self, path, owner):
        self.path = path
        self.owner = owner
        self._fd = None

    def acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = os.read(fd, 200).decode(errors="replace").strip() or "another process"
            finally:
                os.close(fd)
            raise LockHeld(f"the relays are held by {holder} ({self.path}). "
                           f"Stop it first, e.g. sudo systemctl stop smoker")
        os.ftruncate(fd, 0)
        os.write(fd, f"{self.owner} (pid {os.getpid()})".encode())
        self._fd = fd
        return self

    def release(self):
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
