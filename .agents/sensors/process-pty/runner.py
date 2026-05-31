#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shlex
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


@dataclass(frozen=True)
class SensorConfig:
    command: list[str]
    cwd: Path
    env_overrides: dict[str, str]
    artifact_path: Path
    status_path: Path
    max_lines: int
    strip_ansi: bool
    mirror_to_stdout: bool


class RingArtifact:
    def __init__(self, artifact_path: Path, max_lines: int, strip_ansi: bool) -> None:
        self.artifact_path = artifact_path
        self.max_lines = max_lines
        self.strip_ansi = strip_ansi
        self.lines: deque[str] = deque(maxlen=max_lines)
        self._partial = ""
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text("", encoding="utf-8")

    def append_text(self, text: str) -> None:
        if self.strip_ansi:
            text = strip_ansi(text)

        text = self._partial + text
        parts = text.split("\n")
        self._partial = parts.pop()

        for line in parts:
            line = line.rstrip("\r")
            if line:
                self.lines.append(line)

        self.flush()

    def finish(self) -> None:
        if self._partial:
            line = self._partial.rstrip("\r")
            if self.strip_ansi:
                line = strip_ansi(line)
            if line:
                self.lines.append(line)
            self._partial = ""
        self.flush()

    def flush(self) -> None:
        content = "\n".join(self.lines)
        if content:
            content += "\n"
        self.artifact_path.write_text(content, encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    if not isinstance(manifest, dict):
        raise ValueError(f"Manifest must be a mapping: {path}")
    return manifest


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def resolve_path(raw_path: str, manifest_path: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (manifest_path.parent / path).resolve()


def parse_command(raw_command: Any) -> list[str]:
    if isinstance(raw_command, str):
        command = shlex.split(raw_command)
    elif isinstance(raw_command, list) and all(isinstance(item, str) for item in raw_command):
        command = list(raw_command)
    else:
        raise ValueError("source.target.cmd must be a non-empty command string or list of strings")

    if not command:
        raise ValueError("source.target.cmd must not be empty")
    return command


def load_config(manifest_path: Path) -> SensorConfig:
    manifest_path = manifest_path.resolve()
    manifest = load_manifest(manifest_path)

    source = require_mapping(manifest.get("source"), "source")
    if source.get("type") != "process.spawn":
        raise ValueError("process-pty only supports source.type: process.spawn")

    target = require_mapping(source.get("target"), "source.target")
    command = parse_command(target.get("cmd"))

    source_config = require_mapping(source.get("config", {}), "source.config")
    cwd = resolve_path(str(source_config.get("cwd", manifest_path.parent)), manifest_path)
    env_raw = source_config.get("env", {})
    if not isinstance(env_raw, dict):
        raise ValueError("source.config.env must be a mapping")
    env_overrides = {str(key): str(value) for key, value in env_raw.items()}

    sink = require_mapping(manifest.get("sink"), "sink")
    if sink.get("type") != "file.ring":
        raise ValueError("process-pty only supports sink.type: file.ring")

    sink_target = sink.get("target")
    if not isinstance(sink_target, str) or not sink_target:
        raise ValueError("sink.target must be a path string")
    artifact_path = resolve_path(sink_target, manifest_path)

    sink_config = require_mapping(sink.get("config", {}), "sink.config")
    max_lines = int(sink_config.get("max_lines", 2000))
    if max_lines <= 0:
        raise ValueError("sink.config.max_lines must be greater than 0")

    status_target = sink_config.get("status_target", "artifacts/status.json")
    if not isinstance(status_target, str) or not status_target:
        raise ValueError("sink.config.status_target must be a path string")

    return SensorConfig(
        command=list(command),
        cwd=cwd,
        env_overrides=env_overrides,
        artifact_path=artifact_path,
        status_path=resolve_path(status_target, manifest_path),
        max_lines=max_lines,
        strip_ansi=bool(sink_config.get("strip_ansi", True)),
        mirror_to_stdout=bool(sink_config.get("mirror_to_stdout", False)),
    )


def write_status(config: SensorConfig, payload: dict[str, Any]) -> None:
    config.status_path.parent.mkdir(parents=True, exist_ok=True)
    base_payload = {
        "sensor": "process-pty",
        "updated_at": utc_now(),
        "cmd": config.command,
        "cwd": str(config.cwd),
        "artifact_path": str(config.artifact_path),
    }
    base_payload.update(payload)
    config.status_path.write_text(json.dumps(base_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def spawn_under_pty(config: SensorConfig) -> tuple[int, int]:
    env = os.environ.copy()
    env.update(config.env_overrides)

    master_fd, slave_fd = pty.openpty()
    pid = os.fork()

    if pid == 0:
        os.setsid()
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(master_fd)
        os.close(slave_fd)
        os.chdir(config.cwd)
        os.execvpe(config.command[0], config.command, env)
        sys.exit(1)

    os.close(slave_fd)
    return pid, master_fd


def drain_once(master_fd: int, ring: RingArtifact, mirror_to_stdout: bool, size: int = 4096) -> bool:
    try:
        chunk = os.read(master_fd, size)
    except OSError:
        return False
    if not chunk:
        return False

    text = chunk.decode("utf-8", errors="replace")
    ring.append_text(text)
    if mirror_to_stdout:
        sys.stdout.write(text)
        sys.stdout.flush()
    return True


def run(config: SensorConfig) -> int:
    ring = RingArtifact(config.artifact_path, config.max_lines, config.strip_ansi)
    pid, master_fd = spawn_under_pty(config)
    write_status(config, {"state": "running", "pid": pid, "started_at": utc_now(), "exit_code": None})

    def forward_signal(signum: int, _frame: Any) -> None:
        try:
            os.killpg(pid, signum)
            write_status(config, {"state": "stopping", "pid": pid, "signal": signum, "exit_code": None})
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)

    exit_code = 1
    child_reaped = False
    try:
        while True:
            try:
                readable, _, _ = select.select([master_fd], [], [], 0.05)
            except (ValueError, OSError):
                break

            if readable and not drain_once(master_fd, ring, config.mirror_to_stdout):
                break

            try:
                result_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                child_reaped = True
                break

            if result_pid == pid:
                child_reaped = True
                time.sleep(0.1)
                try:
                    readable, _, _ = select.select([master_fd], [], [], 0.1)
                    while readable:
                        if not drain_once(master_fd, ring, config.mirror_to_stdout, size=65536):
                            break
                        readable, _, _ = select.select([master_fd], [], [], 0)
                except OSError:
                    pass
                exit_code = os.waitstatus_to_exitcode(status)
                break
    finally:
        ring.finish()
        try:
            os.close(master_fd)
        except OSError:
            pass

    if not child_reaped:
        try:
            _, status = os.waitpid(pid, 0)
            exit_code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            pass

    write_status(config, {"state": "exited", "pid": pid, "ended_at": utc_now(), "exit_code": exit_code})
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spawn a process under a PTY and mirror stdout/stderr into a file.ring artifact")
    manifest_default = Path(os.environ.get("SENSOR_CONFIG") or Path(__file__).with_name("sensor.yaml"))
    parser.add_argument("--manifest", type=Path, default=manifest_default)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.manifest)
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
