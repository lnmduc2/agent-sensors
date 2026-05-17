#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SensorConfig:
    roots: list[Path]
    include_patterns: list[str]
    scan_interval: float
    idle_sleep: float
    start_position: str
    artifact_path: Path
    artifact_format: str
    max_lines: int
    mirror_to_stdout: bool


@dataclass
class TailState:
    path: Path
    offset: int


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    with manifest_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def build_config(manifest: dict[str, Any]) -> SensorConfig:
    source_config = manifest.get("source", {}).get("config", {})
    sink = manifest.get("sink", {})
    sink_config = sink.get("config", {})

    roots = [Path(root).expanduser() for root in source_config.get("roots", [])]
    include_patterns = list(source_config.get("include_patterns", ["**/*.log"]))
    return SensorConfig(
        roots=roots,
        include_patterns=include_patterns,
        scan_interval=float(source_config.get("scan_interval", 1.0)),
        idle_sleep=float(source_config.get("idle_sleep", 0.2)),
        start_position=str(source_config.get("start_position", "end")),
        artifact_path=Path(sink["target"]).expanduser(),
        artifact_format=str(sink_config.get("format", "text")),
        max_lines=int(sink_config.get("max_lines", 2000)),
        mirror_to_stdout=bool(sink_config.get("mirror_to_stdout", True)),
    )


def candidate_logs(config: SensorConfig) -> list[Path]:
    candidates: dict[str, Path] = {}
    for root in config.roots:
        if not root.exists():
            continue
        for pattern in config.include_patterns:
            for path in root.glob(pattern):
                if not path.is_file():
                    continue
                candidates[str(path)] = path
    return sorted(candidates.values(), key=lambda path: path.stat().st_mtime, reverse=True)


def choose_log(config: SensorConfig) -> Path | None:
    logs = candidate_logs(config)
    return logs[0] if logs else None


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_artifact(config: SensorConfig, payload: dict[str, Any]) -> None:
    ensure_parent(config.artifact_path)
    if config.artifact_format == "json":
        config.artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return

    lines = payload.get("lines", [])
    content = "\n".join(lines[-config.max_lines :])
    if content:
        content += "\n"
    config.artifact_path.write_text(content, encoding="utf-8")


def emit_stdout(lines: list[str]) -> None:
    if not lines:
        return
    sys.stdout.write("".join(lines))
    sys.stdout.flush()


def read_new_lines(state: TailState) -> list[str]:
    if not state.path.exists():
        raise FileNotFoundError(state.path)

    with state.path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, io.SEEK_END)
        size = handle.tell()
        if state.offset > size:
            state.offset = 0
        handle.seek(state.offset)
        lines = handle.readlines()
        state.offset = handle.tell()
        return lines


def init_state(path: Path, start_position: str) -> TailState:
    offset = 0
    if start_position == "end" and path.exists():
        offset = path.stat().st_size
    return TailState(path=path, offset=offset)


def build_payload(active_log: Path | None, status: str, lines: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "active_log": str(active_log) if active_log else None,
        "emitted_line_count": len(lines),
        "lines": [line.rstrip("\n") for line in lines],
    }


def read_seed_lines(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]


def run(manifest_path: Path, once: bool) -> int:
    manifest = load_manifest(manifest_path)
    config = build_config(manifest)

    current_log = choose_log(config)
    state = init_state(current_log, config.start_position) if current_log else None
    artifact_lines = read_seed_lines(current_log, config.max_lines) if current_log else []

    if current_log is None:
        write_artifact(config, build_payload(None, "waiting_for_log", artifact_lines))
    else:
        write_artifact(config, build_payload(current_log, "seeded", artifact_lines))

    last_scan = 0.0

    while True:
        now = time.time()
        if now - last_scan >= config.scan_interval:
            latest_log = choose_log(config)
            last_scan = now
            if latest_log != current_log:
                current_log = latest_log
                state = init_state(current_log, config.start_position) if current_log else None
                artifact_lines = read_seed_lines(current_log, config.max_lines) if current_log else []
                status = "switched_log" if current_log else "waiting_for_log"
                write_artifact(config, build_payload(current_log, status, artifact_lines))

        if state is None:
            if once:
                return 0
            time.sleep(config.idle_sleep)
            continue

        try:
            lines = read_new_lines(state)
        except FileNotFoundError:
            current_log = None
            state = None
            artifact_lines = []
            write_artifact(config, build_payload(None, "waiting_for_log", artifact_lines))
            if once:
                return 0
            time.sleep(config.idle_sleep)
            continue

        if lines:
            new_lines = [line.rstrip("\n") for line in lines]
            if config.start_position == "beginning":
                artifact_lines.extend(new_lines)
            else:
                artifact_lines = new_lines
            artifact_lines = artifact_lines[-config.max_lines :]
            write_artifact(config, build_payload(current_log, "ok", artifact_lines))
            if config.mirror_to_stdout:
                emit_stdout(lines)
            if once:
                return 0
        elif once:
            write_artifact(config, build_payload(current_log, "idle", artifact_lines))
            return 0

        time.sleep(config.idle_sleep)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("sensor.yaml"))
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(run(args.manifest.resolve(), args.once))
