from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


RUNNER_PATH = Path(__file__).resolve().parents[1] / ".agents" / "sensors" / "process-pty" / "runner.py"


def load_runner_module():
    spec = importlib.util.spec_from_file_location("process_pty_runner", RUNNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_manifest(tmp_path: Path, command: list[str] | str) -> Path:
    manifest = {
        "api_version": "sensor/v0.1",
        "name": "process-pty-test",
        "version": "0.1.0",
        "description": "Test process pty sensor",
        "runtime": {"entrypoint": "runner.py", "language": "python"},
        "source": {
            "type": "process.spawn",
            "target": {"cmd": command},
            "config": {
                "cwd": str(tmp_path),
                "env": {"SENSOR_TEST_VALUE": "from-manifest"},
            },
        },
        "sink": {
            "type": "file.ring",
            "target": str(tmp_path / "artifacts" / "stdout.ring"),
            "config": {
                "max_lines": 2,
                "strip_ansi": True,
                "mirror_to_stdout": False,
                "status_target": str(tmp_path / "artifacts" / "status.json"),
            },
        },
        "lifecycle": {"trigger": "manual", "teardown": "on_process_exit"},
        "agent_hints": {"observe_strategy": "Read stdout.ring and status.json"},
    }
    manifest_path = tmp_path / "sensor.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return manifest_path


def test_load_config_reads_process_spawn_and_file_ring_manifest(tmp_path: Path):
    runner = load_runner_module()
    manifest_path = write_manifest(tmp_path, ["python3", "-c", "print('hello')"])

    config = runner.load_config(manifest_path)

    assert config.command == ["python3", "-c", "print('hello')"]
    assert config.cwd == tmp_path
    assert config.env_overrides == {"SENSOR_TEST_VALUE": "from-manifest"}
    assert config.artifact_path == tmp_path / "artifacts" / "stdout.ring"
    assert config.status_path == tmp_path / "artifacts" / "status.json"
    assert config.max_lines == 2
    assert config.strip_ansi is True
    assert config.mirror_to_stdout is False


def test_load_config_accepts_shell_like_command_string(tmp_path: Path):
    runner = load_runner_module()
    manifest_path = write_manifest(tmp_path, "python3 -c 'print(123)'")

    config = runner.load_config(manifest_path)

    assert config.command == ["python3", "-c", "print(123)"]


def test_ring_buffer_keeps_only_recent_lines_and_strips_ansi(tmp_path: Path):
    runner = load_runner_module()
    artifact_path = tmp_path / "stdout.ring"
    ring = runner.RingArtifact(artifact_path=artifact_path, max_lines=2, strip_ansi=True)

    ring.append_text("\x1b[31mred\x1b[0m\nsecond\nthird\n")

    assert artifact_path.read_text(encoding="utf-8") == "second\nthird\n"


def test_runner_spawns_manifest_command_writes_ring_and_status(tmp_path: Path):
    command = "python3 -c \"import os; print(os.environ['SENSOR_TEST_VALUE']); print('line2'); print('line3')\""
    expected_command = [
        "python3",
        "-c",
        "import os; print(os.environ['SENSOR_TEST_VALUE']); print('line2'); print('line3')",
    ]
    manifest_path = write_manifest(tmp_path, command)

    result = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--manifest", str(manifest_path)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "artifacts" / "stdout.ring").read_text(encoding="utf-8") == "line2\nline3\n"
    status = yaml.safe_load((tmp_path / "artifacts" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    assert status["cmd"] == expected_command
    assert status["artifact_path"] == str(tmp_path / "artifacts" / "stdout.ring")
