# Sensor Specification
**Version:** 0.1-draft  
**Status:** Concept  

---

## 1. Motivation

AI agents (Cursor, Claude Code, Copilot, etc.) operate within a **fixed tool harness** — a predefined set of capabilities the agent vendor ships. When an agent needs to observe a runtime resource that falls outside that harness (a running process, a hardware signal, a live network stream, a VM's internal state), it has no path to do so.

Existing workarounds are inadequate:

- **MCP Server** — requires vendor-defined protocol, agent must have MCP support built-in
- **System prompt stuffing** — static, not live, not scalable
- **Manual copy-paste** — breaks automation
- **Custom agent fork** — not feasible for closed agents (Cursor, etc.)

**Sensor** is a pattern that solves this gap without modifying the agent or its harness.

---

## 2. Core Concept

> A **Sensor** is a user-owned process that attaches to a runtime resource and continuously transcribes its state into an **observable artifact** — a representation the agent already knows how to consume with its native capabilities.

```
Runtime Resource
      │
      │  (attach / poll / subscribe)
      ▼
  [ Sensor ]
      │
      │  (write / emit)
      ▼
Observable Artifact  ────────────────────►  Agent
                      (read / search /
                       fetch / query)
```

Three components:

| Component | Description |
|---|---|
| **Runtime Resource** | Anything with live state: process, socket, device, API, DB cursor, etc. |
| **Sensor** | The user-owned process that bridges resource → artifact |
| **Observable Artifact** | A continuously updated representation the agent can already observe natively |

---

## 3. Sensor Anatomy

Each Sensor is defined by a `sensor.yaml` manifest alongside a `runner.*` file.

```yaml
# sensor.yaml

# --- Identity ---
api_version: string   # manifest schema version, e.g. "sensor/v0.1"
name: string          # unique within project, kebab-case
version: semver       # sensor's own version, e.g. "1.0.0"
description: string   # what this sensor does, in one sentence

# --- Runtime ---
runtime:
  entrypoint: string  # e.g. runner.py, runner.sh, runner.js
  language: enum      # python | node | bash | binary

# --- Source: what runtime resource to attach to ---
source:
  type: enum              # see Source Types below
  target: string|object   # where the sensor attaches
  credentials: object     # optional secret references (env/file/etc.)
  config: object          # optional source-specific config

# --- Sink: where to expose the observable artifact ---
sink:
  type: enum              # see Sink Types below
  target: string|object   # where the artifact is exposed
  credentials: object     # optional secret references (env/file/etc.)
  config: object          # optional sink-specific config

# --- Lifecycle ---
lifecycle:
  trigger: enum       # manual | always_on
  teardown: enum      # on_process_exit | manual

# --- Agent Hints ---
agent_hints:
  observe_strategy: string  # how the agent should observe the artifact
  query_examples:           # optional example queries agent can run
    - string
  freshness_sla: duration   # how stale the artifact can be (e.g. "5s")
```

---

## 4. Source Types

Like sinks, sources use a small fixed shape:

- `type` — which source behavior to use
- `target` — where the sensor attaches
- `credentials` — optional secret references, if the source needs auth
- `config` — optional source-specific config

`target` identifies the runtime resource itself. `config` describes how to attach, poll, filter, or interpret that resource.

| Type | Description | Example `target` | Example `config` |
|---|---|---|---|
| `process.spawn` | spawn a command and capture its output without mutating an existing process | `cmd: ["dotnet", "run"]` | `capture: pty`, `streams: [stdout, stderr]`, `cwd: /app`, `env: {...}` |
| `file.tail` | tail of a file growing on disk | `/var/log/app.log` | `poll_interval: 250ms` |
| `socket.tcp` | bytes off a TCP socket | `host: localhost, port: 9229` | `{}` |
| `http.poll` | poll an HTTP endpoint at interval | `https://...` | `interval: 2s` |
| `device.serial` | serial port / hardware sensor | `/dev/ttyUSB0` | `baud: 9600` |
| `db.cursor` | live query result from a DB | `dsn: postgres://...` | `query: select ...` |
| `event.bus` | subscribe to an event stream | `broker: redis` | `channel: ...` |
| `custom` | arbitrary script/binary as source | `./my_source.sh` | `{}` |

Source types are **extensible** — implementors can define new types.

`process.spawn` is the safe default for process output sensors. Sensors should start the process they observe whenever possible, instead of attaching to a supervisor-owned or already-running process and risking changes to its stdio, TTY, session, or IPC state.

---

## 5. Sink Types

The sink defines what **observable artifact** the agent receives. The key constraint: **the agent must already know how to observe it with native capabilities, without any new protocol or tool**.

| Type | Artifact | Agent observes via |
|---|---|---|
| `file.append` | append-only flat file | native read/search tools |
| `file.ring` | fixed-size ring buffer file (last N lines) | native read/search tools |
| `file.snapshot` | single file, overwritten on each update | native read tools |
| `http.endpoint` | HTTP endpoint agent can fetch | native fetch tools |
| `db.table` | queryable DB table | native SQL/query tools |

**Default recommended sink:** `file.ring` — bounded size prevents runaway growth while preserving the freshest recent output.

Each sink has a small fixed shape:

- `type` — which sink behavior to use
- `target` — where the artifact is exposed
- `credentials` — optional secret references, if the sink needs auth
- `config` — optional sink-specific config

This keeps the manifest readable for agents and humans while avoiding sink-specific top-level sprawl.

```yaml
sink:
  type: file.ring
  target: ./pencil.log
  config:
    max_lines: 2000
    flush_interval: 500ms
    format: text

sink:
  type: file.append
  target: ./latency.jsonl
  config:
    format: jsonl

sink:
  type: file.snapshot
  target: ./disk.json
  config:
    flush_interval: 5s
    format: json

sink:
  type: http.endpoint
  target:
    port: 7788
    route: /sensor/pencil
  config:
    cors: true

sink:
  type: db.table
  target:
    dsn: postgres://localhost:5432/mydb
    table: sensor_pencil
  credentials:
    source: env
    env:
      password: DB_PASSWORD
  config:
    batch_size: 100
```

**Required fields by sink type:**

| Sink type | Required fields inside `target` |
|---|---|
| `file.append` | path string |
| `file.ring` | path string |
| `file.snapshot` | path string |
| `http.endpoint` | `port`, `route` |
| `db.table` | `dsn`, `table` |

`credentials` is optional and should contain only references to secrets, not inline secret values.
`config` is optional and contains sink-specific behavior such as retention, flush interval, or artifact format.

---

## 6. Lifecycle

```
trigger: manual       → Sensor starts when user explicitly starts it
trigger: always_on    → Sensor starts under a long-running supervisor (systemd, launchd, etc.)

teardown: on_process_exit → Sensor stops when source process exits
teardown: manual          → Sensor runs until user stops it
```

---

## 7. Agent Hints Block

This is the **critical differentiator** from a plain sidecar script. The `agent_hints` block is readable by the agent and tells it:

- **How to observe the artifact** without hallucinating a strategy
- **How fresh the data is** so it knows whether to re-read
- **Example queries** it can immediately try

```yaml
agent_hints:
  observe_strategy: >
    This artifact is continuously updated.
    Use the agent's native read, search, fetch, or query capabilities
    to inspect recent state without introducing any new protocol.
  query_examples:
    - "Search recent output for ERROR events"
    - "Read the latest 100 lines or the latest snapshot"
  freshness_sla: "1s"
```

---

## 8. Folder Convention

A Sensor lives as a self-contained folder. The folder structure is standardized so any agent or human can understand it without prior context:

```
.agents/sensors/
├── pencil-stdout/
│   ├── sensor.yaml       # manifest — identity, source, sink, agent_hints
│   └── runner.py         # logic — attach to source, write to sink
├── disk-usage/
│   ├── sensor.yaml
│   └── runner.sh
└── api-latency/
    ├── sensor.yaml
    └── runner.js
```

**Rules:**
- One folder = one Sensor
- `sensor.yaml` is always required
- `runner.*` is the entrypoint — any language, declared in manifest
- Folder name matches `name` field in `sensor.yaml`

**Sink target** is declared in `sensor.yaml` — anywhere in the project, as an absolute path, or as a network/database locator depending on sink type. No enforced convention.

**Shareability:** A Sensor folder is portable as a manifest + runner package. Share it by copying the folder, or hosting it as a public GitHub repo. No registry required — a Sensor is just a folder with a `sensor.yaml`. Runtime dependencies and permissions remain environment-specific.

---

## 9. Difference from Existing Patterns

| Pattern | Who owns it | Agent needs | Live data | User can modify |
|---|---|---|---|---|
| MCP Server | Vendor | MCP harness | ✅ | ❌ (vendor code) |
| Agent Skill / SKILL.md | User | Native read tools | ❌ (static) | ✅ |
| RAG / context stuffing | User | Nothing | ❌ (batch) | ✅ |
| **Sensor** | **User** | **Native read/search/fetch/query tools** | **✅** | **✅** |

The Sensor pattern occupies a unique cell: **user-owned + continuously observable live data + no new agent capability required**.

---

## 10. Reference Implementation (minimal)

A minimal Sensor for `file.tail → file.ring` (continuous file-based example):

```python
#!/usr/bin/env python3

import collections
import time
from pathlib import Path


def run_sensor(manifest: dict):
    source = manifest["source"]
    sink = manifest["sink"]

    if source["type"] != "file.tail":
        raise NotImplementedError("minimal example only supports file.tail")

    in_path = Path(source["target"])
    out_path = Path(sink["target"])
    source_config = source.get("config", {})
    sink_config = sink.get("config", {})
    max_lines = sink_config.get("max_lines", 2000)
    poll_interval = source_config.get("poll_interval", 0.25)
    buf = collections.deque(maxlen=max_lines)

    with in_path.open("r", encoding="utf-8", errors="replace") as fd:
        fd.seek(0, 2)

        while True:
            line = fd.readline()
            if not line:
                time.sleep(poll_interval)
                continue

            buf.append(line.rstrip("\n"))
            out_path.write_text("\n".join(buf), encoding="utf-8")
```

---

## 11. Open Questions (v0.1)

- [ ] **Security model** — Sensor runs as user, but attaching to `/proc/<pid>/fd` may need elevated perms. How to scope permissions?
- [ ] **Windows support** — `/proc` is Linux. Win32 equivalent for stdout tap?
- [ ] **Authentication model** — If source or sink needs auth, what secret reference formats should be standardized?
- [ ] **Multi-agent** — Can multiple agents share one Sensor? Concurrency on the artifact?
- [ ] **Sensor composition** — Can a Sensor consume another Sensor's artifact as its source?

---

*Sensor is a user-space pattern, not a protocol. Any language, any runtime.*
