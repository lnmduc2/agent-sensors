#!/usr/bin/env python3
"""
Sensor: port-process-capture
Source: process.port  — finds PID listening on a TCP port, captures stdout/stderr
Sink:   file.ring     — fixed-size ring buffer written to a local file

Capture strategy (tried in order):
  1. ALL requested fds → regular files  →  MultiFileCapture (one thread per file)
  2. strace available                   →  StraceCapture (one reader thread)
  3. Neither                            →  RuntimeError with actionable message

Both strategies push (stream_label, line) tuples into a shared queue.Queue so
stdout and stderr are captured concurrently — no stream ever blocks the other.

Usage:
  python runner.py [sensor.yaml]
  python runner.py --port 8080 --output ./out.log [--max-lines 2000]
"""

from __future__ import annotations

import argparse
import collections
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

# Item pushed into the shared capture queue.
# sentinel None signals that one reader thread has finished.
CaptureItem = Tuple[str, str]   # (stream_label, line)
_SENTINEL = None


# ─────────────────────────────────────────────────────────────────────────────
# Port → PID resolution  (Linux /proc/net/tcp)
# ─────────────────────────────────────────────────────────────────────────────

def _read_proc_net(path: str) -> dict[int, int]:
    """Parse /proc/net/tcp[6], return {socket_inode: local_port}."""
    result: dict[int, int] = {}
    try:
        with open(path, encoding="ascii") as f:
            for line in f.readlines()[1:]:       # skip header row
                parts = line.split()
                if len(parts) < 10:
                    continue
                _ip, hex_port = parts[1].split(":")
                port = int(hex_port, 16)
                inode = int(parts[9])
                result[inode] = port
    except FileNotFoundError:
        pass
    return result


def find_pid_for_port(port: int) -> Optional[int]:
    """
    Walk /proc/net/tcp and /proc/net/tcp6 to find which process is
    listening on *port*, then return its PID (or None if not found).
    """
    inode_to_port: dict[int, int] = {}
    for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        inode_to_port.update(_read_proc_net(tcp_file))

    target_inodes = {inode for inode, p in inode_to_port.items() if p == port}
    if not target_inodes:
        return None

    # Match inodes against /proc/<pid>/fd/* socket symlinks
    for pid_path in Path("/proc").glob("[0-9]*"):
        fd_dir = pid_path / "fd"
        try:
            for fd_entry in fd_dir.iterdir():
                try:
                    link = os.readlink(fd_entry)            # e.g. "socket:[12345]"
                    if link.startswith("socket:["):
                        inode = int(link[8:-1])
                        if inode in target_inodes:
                            return int(pid_path.name)
                except (OSError, ValueError):
                    continue
        except (PermissionError, FileNotFoundError):
            continue

    return None


def pid_is_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Capture strategies
# ─────────────────────────────────────────────────────────────────────────────

class MultiFileCapture:
    """
    Strategy A: every requested fd points to a regular file on disk.

    Spawns one daemon thread per *unique* file path so stdout and stderr are
    tailed concurrently and neither blocks the other.  If both fds resolve to
    the same path (e.g. 2>&1) only one thread is created and lines are labelled
    "mixed".
    """

    def __init__(self, file_map: dict[str, str], poll_interval: float) -> None:
        # file_map: {"stdout": "/path/a", "stderr": "/path/b"}
        self._file_map = file_map
        self._poll = poll_interval
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def thread_count(self) -> int:
        return len(self._threads)

    def start(self, q: queue.Queue) -> None:
        # Deduplicate: group stream names by their resolved file path
        path_to_streams: dict[str, list[str]] = {}
        for stream, path in self._file_map.items():
            path_to_streams.setdefault(path, []).append(stream)

        for path, streams in path_to_streams.items():
            label = streams[0] if len(streams) == 1 else "mixed"
            t = threading.Thread(
                target=self._tail,
                args=(path, label, q),
                daemon=True,
                name=f"tail-{label}",
            )
            self._threads.append(t)
            t.start()
            log(f"MultiFileCapture: tailing {path!r}  [{label}]")

    def _tail(self, path: str, label: str, q: queue.Queue) -> None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)                        # start from current end
                while not self._stop.is_set():
                    line = f.readline()
                    if line:
                        q.put((label, line.rstrip("\n")))
                    else:
                        time.sleep(self._poll)
        except OSError as exc:
            log(f"MultiFileCapture error on {path!r}: {exc}")
        finally:
            q.put(_SENTINEL)

    def stop(self) -> None:
        self._stop.set()


class StraceCapture:
    """
    Strategy B: one or more fds are pipes or ttys (typical for server processes).

    Attaches strace to intercept write(1,…) / write(2,…) syscalls.  A single
    strace process captures both fds; a single reader thread pushes labelled
    lines into the shared queue.  stdout and stderr naturally interleave in
    arrival order, mirroring exactly what you would see in a terminal.

    strace output format for fd 1:   write(1, "hello\\n", 6) = 6
    strace output format for fd 2:   write(2, "error\\n", 6) = 6

    Requires: strace installed + CAP_SYS_PTRACE or ptrace_scope == 0.
    """

    # Group 1 = fd number ("1" or "2"), Group 2 = C-string payload
    _WRITE_RE = re.compile(r'write\(([12]), ("(?:[^"\\]|\\.)*"), \d+\)\s*=\s*\d+')
    _FD_LABEL  = {"1": "stdout", "2": "stderr"}

    def __init__(self, pid: int, fds: list[int]) -> None:
        self._pid = pid
        self._fds = fds
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def thread_count(self) -> int:
        return 1

    def start(self, q: queue.Queue) -> None:
        fd_filter = ",".join(str(f) for f in self._fds)
        cmd = [
            "strace",
            "-p", str(self._pid),
            "-e", "trace=write",
            "-e", f"write={fd_filter}",   # only dump payloads for our fds
            "-s", "8192",                 # max payload chars per write call
            "-q",                         # no "attached"/"detached" noise
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,     # strace sends its output to stderr
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(
            target=self._reader,
            args=(q,),
            daemon=True,
            name="strace-reader",
        )
        self._thread.start()
        log(f"StraceCapture: attached to PID {self._pid}, fds={self._fds}")

    def _reader(self, q: queue.Queue) -> None:
        """Runs in its own thread; parses strace lines and pushes to queue."""
        try:
            while not self._stop.is_set():
                # Ép Python đọc chuẩn chỉ từng dòng một, không gom cụm dữ liệu
                raw = self._proc.stdout.readline()
                if not raw:
                    break
                
                for m in self._WRITE_RE.finditer(raw):
                    label   = self._FD_LABEL.get(m.group(1), "stdout")
                    content = _decode_c_string(m.group(2))
                    for line in content.split("\n"):
                        stripped = line.rstrip("\r")
                        if stripped:
                            q.put((label, stripped))
        finally:
            q.put(_SENTINEL)

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def _decode_c_string(s: str) -> str:
    """
    Convert strace's C-escaped string literal (with surrounding quotes)
    back to a plain Python string.
    e.g.  '"hello\\nworld"'  →  'hello\nworld'
    """
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    try:
        return s.encode("raw_unicode_escape").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return s


def _fd_symlink_target(pid: int, fd: int) -> Optional[str]:
    try:
        return os.readlink(f"/proc/{pid}/fd/{fd}")
    except OSError:
        return None


def make_capturer(pid: int, streams: list[str], poll_interval: float):
    """
    Choose the best capture strategy for the process, in priority order:
      1. ALL requested fds → regular files  →  MultiFileCapture (thread per file)
      2. strace available                   →  StraceCapture (one reader thread)
      3. Raise with an actionable error message
    """
    fd_map = {"stdout": 1, "stderr": 2}
    requested = {s: fd_map[s] for s in streams if s in fd_map}

    # Strategy A: check if EVERY stream fd resolves to a regular file
    file_map: dict[str, str] = {}
    for stream, fd in requested.items():
        target = _fd_symlink_target(pid, fd)
        if target and os.path.isfile(target):
            file_map[stream] = target

    if len(file_map) == len(requested):
        return MultiFileCapture(file_map, poll_interval)

    # Strategy B: at least one fd is a pipe/tty — use strace
    if shutil.which("strace"):
        return StraceCapture(pid, list(requested.values()))

    # Nothing worked — give the user actionable guidance
    targets = {s: _fd_symlink_target(pid, fd) or "?" for s, fd in requested.items()}
    raise RuntimeError(
        f"Cannot capture output of PID {pid}.\n"
        f"  fd targets: {targets}\n"
        f"  Pipes/ttys cannot be read without strace.\n"
        f"\n"
        f"Solutions:\n"
        f"  1. Install strace:  sudo apt install strace  (or dnf/pacman equivalent)\n"
        f"  2. Relax ptrace scope:  echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope\n"
        f"  3. Run the target process with output redirected to a file, then use "
        f"source.type=file.tail instead."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sink: file.ring
# ─────────────────────────────────────────────────────────────────────────────

class FileRingSink:
    """
    Fixed-size ring buffer flushed to disk at a configurable interval.
    The file always contains at most *max_lines* of the most recent output.
    """

    def __init__(self, path: str, max_lines: int, flush_interval: float) -> None:
        self._path = Path(path)
        self._buf: collections.deque[str] = collections.deque(maxlen=max_lines)
        self._flush_interval = flush_interval
        self._last_flush = 0.0
        self._dirty = False

    def write(self, line: str) -> None:
        self._buf.append(line)
        self._dirty = True
        now = time.monotonic()
        if now - self._last_flush >= self._flush_interval:
            self._flush()
            self._last_flush = now

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("\n".join(self._buf) + "\n", encoding="utf-8")
        self._dirty = False

    def close(self) -> None:
        self._flush()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_duration(value: str | float | int) -> float:
    """Parse '250ms', '1s', '0.5', etc. → seconds (float)."""
    s = str(value).strip()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


def log(msg: str) -> None:
    print(f"[sensor] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Main sensor loop
# ─────────────────────────────────────────────────────────────────────────────

def format_line(stream: str, line: str, label: bool) -> str:
    """Optionally prefix each line with its stream label, like a terminal would show."""
    if not label:
        return line
    prefix = {"stdout": "[OUT]", "stderr": "[ERR]", "mixed": "     "}.get(stream, "[???]")
    return f"{prefix} {line}"


# ─────────────────────────────────────────────────────────────────────────────
# Main sensor loop
# ─────────────────────────────────────────────────────────────────────────────

def run_sensor(manifest: dict) -> None:
    source = manifest["source"]
    sink_conf = manifest["sink"]

    # --- Source config ---
    port = int(source["target"])
    src_cfg = source.get("config", {})
    streams: list[str] = src_cfg.get("streams", ["stdout", "stderr"])
    poll_interval = parse_duration(src_cfg.get("poll_interval", "250ms"))
    label_streams: bool = src_cfg.get("label_streams", False)

    # --- Sink config ---
    sink_target: str = sink_conf["target"]
    snk_cfg = sink_conf.get("config", {})
    max_lines = int(snk_cfg.get("max_lines", 2000))
    flush_interval = parse_duration(snk_cfg.get("flush_interval", "500ms"))

    sink = FileRingSink(sink_target, max_lines, flush_interval)
    capturer = None

    def _shutdown(signum, frame):
        log("Shutdown signal received.")
        if capturer:
            capturer.stop()
        sink.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # --- Phase 1: wait for a process to appear on the port ---
    log(f"Waiting for a process on port {port}…")
    pid: Optional[int] = None
    while pid is None:
        pid = find_pid_for_port(port)
        if pid is None:
            time.sleep(poll_interval)

    # Read process name for nicer logging
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        comm = "unknown"
    log(f"Attached to PID={pid} ({comm}) on port {port}")
    log(f"Capturing: {streams}  →  {sink_target}"
        + ("  [stream labels ON]" if label_streams else ""))

    # --- Phase 2: pick capture strategy and start background threads ---
    q: queue.Queue = queue.Queue()
    capturer = make_capturer(pid, streams, poll_interval)
    capturer.start(q)
    active_readers = capturer.thread_count

    # --- Phase 3: drain queue — stdout and stderr arrive concurrently ---
    try:
        while True:
            try:
                item = q.get(timeout=poll_interval)
            except queue.Empty:
                # No data — check if the process is still alive
                if not pid_is_alive(pid):
                    log(f"PID {pid} exited. Sensor stopping.")
                    break
                continue

            if item is _SENTINEL:
                # One reader thread finished
                active_readers -= 1
                if active_readers <= 0:
                    log("All capture threads finished.")
                    break
                continue

            stream, line = item
            sink.write(format_line(stream, line, label_streams))

    except KeyboardInterrupt:
        log("Interrupted by user.")
    finally:
        capturer.stop()
        sink.close()
        log(f"Output written to {sink_target!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point  (manifest mode  OR  quick CLI mode)
# ─────────────────────────────────────────────────────────────────────────────

def load_manifest(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit(
            "PyYAML is required:  pip install pyyaml\n"
            "Or use CLI flags:    python runner.py --port PORT --output FILE"
        )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def cli_to_manifest(args: argparse.Namespace) -> dict:
    """Build a minimal manifest dict from CLI arguments."""
    return {
        "source": {
            "type": "process.port",
            "target": args.port,
            "config": {
                "streams": args.streams,
                "poll_interval": args.poll_interval,
                "label_streams": args.label_streams,
            },
        },
        "sink": {
            "type": "file.ring",
            "target": args.output,
            "config": {
                "max_lines": args.max_lines,
                "flush_interval": args.flush_interval,
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="port-process-capture sensor — attach to a port, capture stdout/stderr",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use sensor.yaml manifest (default)
  python runner.py

  # Override manifest path
  python runner.py sensor.yaml

  # Quick CLI mode (no YAML needed)
  python runner.py --port 8080 --output ./app.log
  python runner.py --port 3000 --output /tmp/dev.log --streams stdout --max-lines 500
""",
    )
    parser.add_argument("manifest", nargs="?", help="Path to sensor.yaml (default: ./sensor.yaml)")
    parser.add_argument("--port", type=int, help="TCP port to watch (CLI mode)")
    parser.add_argument("--output", help="Output file path (CLI mode)")
    parser.add_argument("--streams", nargs="+", default=["stdout", "stderr"],
                        choices=["stdout", "stderr"], metavar="STREAM",
                        help="Streams to capture (default: stdout stderr)")
    parser.add_argument("--label-streams", action="store_true",
                        help="Prefix each line with [OUT] or [ERR] to distinguish streams")
    parser.add_argument("--max-lines", type=int, default=2000, help="Ring buffer size (default: 2000)")
    parser.add_argument("--poll-interval", default="250ms", help="Poll interval (default: 250ms)")
    parser.add_argument("--flush-interval", default="500ms", help="Flush interval (default: 500ms)")

    args = parser.parse_args()

    # CLI mode: --port and --output both provided, no manifest needed
    if args.port and args.output:
        manifest = cli_to_manifest(args)
    else:
        # Manifest mode
        manifest_path = args.manifest or (Path(__file__).parent / "sensor.yaml")
        manifest = load_manifest(str(manifest_path))
        # CLI flags override manifest fields if provided
        if args.port:
            manifest["source"]["target"] = args.port
        if args.output:
            manifest["sink"]["target"] = args.output

    run_sensor(manifest)


if __name__ == "__main__":
    main()
