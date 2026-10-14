"""LaunchAgent entry: bound process-owned output logs before calling the normal run."""

import fcntl
import io
import os
import re
import stat
import sys
import threading
from pathlib import Path

from . import cli as hg_cli
from . import file_io as hg_file_io
from . import runtime as hg_runtime

MAX_BYTES = 1_000_000
BACKUPS = 3


def regular_fd(path, flags, mode=0o600):
    flags |= getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    fd = os.open(path, flags, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        os.fchmod(fd, mode)
        return fd
    except BaseException:
        os.close(fd)
        raise


def checked_paths(path):
    """Reject every existing numbered generation before changing any of them."""
    paths = [path]
    pattern = re.compile(re.escape(path.name) + r"\.[0-9]+\Z")
    paths.extend(item for item in path.parent.iterdir() if pattern.fullmatch(item.name))
    for item in paths:
        try:
            mode = item.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("invalid log path")
    return paths


def write_all(fd, data):
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("short log write")
        offset += written


class BoundedStream(io.TextIOBase):
    """Unbuffered UTF-8 output, with each generation bounded by encoded bytes."""

    def __init__(self, path, max_bytes=MAX_BYTES, backups=BACKUPS, on_failure=None):
        super().__init__()
        if max_bytes < 4 or backups < 0:
            raise ValueError("invalid log limits")
        self.path = Path(path)
        self.max_bytes, self.backups = max_bytes, backups
        self.on_failure = on_failure
        self.failed = False
        self._fd = None
        self._lock = threading.RLock()
        self._size = 0
        try:
            paths = checked_paths(self.path)
            allowed = {self.path, *(self.backup(i) for i in range(1, backups + 1))}
            for item in paths:
                if not item.exists():
                    continue
                if item not in allowed:
                    item.unlink()
                    continue
                fd = regular_fd(item, os.O_RDWR)
                try:
                    size = os.fstat(fd).st_size
                    os.lseek(fd, max(0, size - max_bytes), os.SEEK_SET)
                    tail = bytearray()
                    while len(tail) < min(size, max_bytes):
                        block = os.read(fd, min(16384, min(size, max_bytes) - len(tail)))
                        if not block:
                            break
                        tail.extend(block)
                    tail = tail.decode("utf-8", errors="ignore").encode("utf-8")
                    if size > max_bytes or len(tail) != size:
                        os.lseek(fd, 0, os.SEEK_SET)
                        write_all(fd, tail)
                        os.ftruncate(fd, len(tail))
                finally:
                    os.close(fd)
            self._open()
        except BaseException:
            self.close()
            raise

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"

    def writable(self):
        return True

    def backup(self, number):
        return self.path.with_name(f"{self.path.name}.{number}")

    def _open(self):
        self._fd = regular_fd(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        self._size = os.fstat(self._fd).st_size

    def _rotate(self):
        checked_paths(self.path)
        fd, self._fd = self._fd, None
        os.close(fd)
        if self.backups:
            self.backup(self.backups).unlink(missing_ok=True)
            for number in range(self.backups - 1, 0, -1):
                source = self.backup(number)
                if source.exists():
                    source.replace(self.backup(number + 1))
            self.path.replace(self.backup(1))
        else:
            self.path.unlink()
        self._open()

    def _mark_failed(self, exc):
        if not self.failed:
            self.failed = True
            if self.on_failure is not None:
                self.on_failure(self, type(exc).__name__)

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("expected text")
        if self.closed or self.failed:
            return len(text)
        failure = None
        with self._lock:
            try:
                # Encode bounded chunks, even when a caller supplies one enormous string.
                for start in range(0, len(text), 16384):
                    data = text[start : start + 16384].encode("utf-8", errors="replace")
                    while data:
                        cut = min(self.max_bytes - self._size, len(data))
                        if cut < len(data):
                            while cut and data[cut] & 0xC0 == 0x80:
                                cut -= 1
                        if not cut:
                            self._rotate()
                            continue
                        try:
                            write_all(self._fd, data[:cut])
                        except Exception:
                            # A short write can leave half a codepoint. Keep only the
                            # previous complete chunk when the filesystem permits it.
                            try:
                                os.ftruncate(self._fd, self._size)
                            except Exception:
                                pass
                            raise
                        self._size += cut
                        data = data[cut:]
            except Exception as exc:
                failure = exc
        if failure is not None:
            self._mark_failed(failure)
        return len(text)

    def flush(self):
        if not self.closed and not self.failed and self._fd is not None:
            try:
                # os.write has no userspace buffer; check that the owned fd is still usable.
                os.fstat(self._fd)
            except Exception as exc:
                self._mark_failed(exc)

    def close(self):
        if not self.closed:
            self.flush()
            if self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    os.close(fd)
                except Exception as exc:
                    self._mark_failed(exc)
            super().close()


def safe_notice(stream, category, message="后台输出日志失败"):
    try:
        stream.write(f"{message}：{category}。\n")
        stream.flush()
    except Exception:
        pass


def run_locked(on_ready=None):
    original_out, original_err, original_argv = sys.stdout, sys.stderr, sys.argv
    streams = []
    reporting = False

    def failed(stream, category):
        nonlocal reporting
        if reporting:
            return
        reporting = True
        try:
            err = streams[1] if len(streams) > 1 else None
            target = (
                err
                if err is not stream and err is not None and not err.failed and not err.closed
                else original_err
            )
            safe_notice(target, category)
        finally:
            reporting = False

    try:
        # Preflight both destinations before normalizing either output file.
        for name in ("agent.out.log", "agent.err.log"):
            checked_paths(hg_runtime.STATE / name)
        for name in ("agent.out.log", "agent.err.log"):
            streams.append(BoundedStream(hg_runtime.STATE / name, on_failure=failed))
    except Exception as exc:
        for stream in streams:
            stream.close()
        safe_notice(original_err, type(exc).__name__)
        return 2
    except BaseException:
        for stream in streams:
            stream.close()
        raise
    try:
        sys.stdout, sys.stderr = streams
        sys.argv = ["health_garmin_sync", "run"]
        try:
            if on_ready is not None:
                on_ready()
            result = hg_cli.main()
        except BaseException as exc:
            safe_notice(streams[1], type(exc).__name__, message="后台运行失败")
            raise
    finally:
        sys.stdout, sys.stderr, sys.argv = original_out, original_err, original_argv
        for stream in streams:
            stream.close()
    return 2 if result == 0 and any(stream.failed for stream in streams) else result


def finish_agent_attempt(attempt, preserve_error=False):
    try:
        attempt["finished_at"] = hg_runtime.now().isoformat()
        hg_file_io.write_json(hg_runtime.STATE / "last_agent_attempt.json", attempt)
    except BaseException as exc:
        if not isinstance(exc, Exception) and not preserve_error:
            raise
        safe_notice(sys.stderr, type(exc).__name__, message="后台入口结束证据未保存")
        return False
    return True


def run_with_agent_attempt():
    """Caller holds agent-log.lock through setup, stream cleanup and evidence writes."""
    attempt = {
        "outcome": "running",
        "started_at": hg_runtime.now().isoformat(),
        "finished_at": None,
        "stage": "setup",
        "error_type": None,
    }
    try:
        hg_file_io.write_json(hg_runtime.STATE / "last_agent_attempt.json", attempt)
    except BaseException as exc:
        safe_notice(sys.stderr, type(exc).__name__, message="后台入口开始证据未保存")
        raise

    def ready():
        next_stage = dict(attempt, stage="run")
        hg_file_io.write_json(hg_runtime.STATE / "last_agent_attempt.json", next_stage)
        attempt.update(next_stage)

    try:
        result = run_locked(on_ready=ready)
    except KeyboardInterrupt as exc:
        attempt.update(outcome="interrupted", error_type=type(exc).__name__)
        finish_agent_attempt(attempt, preserve_error=True)
        raise
    except Exception as exc:
        attempt.update(outcome="failed", error_type=type(exc).__name__)
        finish_agent_attempt(attempt, preserve_error=True)
        raise
    else:
        if result == 0:
            attempt.update(outcome="completed", error_type=None)
        elif result == 130:
            attempt.update(outcome="interrupted", error_type="KeyboardInterrupt")
        else:
            attempt.update(outcome="failed", error_type="CommandFailed")
        saved = finish_agent_attempt(attempt, preserve_error=result != 0)
        return 2 if result == 0 and not saved else result


def main():
    try:
        hg_file_io.private_dir(hg_runtime.STATE)
        lock_fd = regular_fd(hg_runtime.STATE / "agent-log.lock", os.O_WRONLY | os.O_CREAT)
    except Exception as exc:
        safe_notice(sys.stderr, type(exc).__name__)
        return 2
    result = None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result = 0
        except Exception as exc:
            safe_notice(sys.stderr, type(exc).__name__)
            result = 2
        else:
            result = run_with_agent_attempt()
    finally:
        try:
            os.close(lock_fd)
        except Exception as exc:
            safe_notice(sys.stderr, type(exc).__name__)
            if result == 0:
                result = 2
    return result


def entrypoint():
    try:
        return main()
    except KeyboardInterrupt:
        return 130
    except Exception:
        # run_locked already recorded the safe category while output was redirected.
        return 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
