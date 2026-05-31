---
name: use-sensors
description: Use when consuming Agent Sensor artifacts, reading sensor.yaml manifests, inspecting file.ring/file.snapshot/http/db sinks, checking agent_hints freshness_sla, or deciding whether to ask the user to activate or restart sensors.
---

# Use Sensors

## Overview

Agents are **consumers of Observable Artifacts**, not owners of Sensor lifecycle. A Sensor is a user-owned bridge from a Runtime Resource to an artifact the agent can already observe with native tools.

Your job is to read the manifest, observe the declared sink target, judge freshness against `agent_hints`, and keep the user informed when the artifact is stale, missing, or insufficient.

## Mental Model

```
Runtime Resource -> Sensor -> Observable Artifact -> Agent
```

You are on the **right side** of this pipeline:

| Thing | Owner | Agent role |
|---|---|---|
| Runtime Resource | User/system | Do not attach directly unless explicitly asked |
| Sensor runner (`runner.py`, `runner.sh`, `runner.js`) | User-owned process | Do not invoke directly by default |
| Observable Artifact (`file.ring`, `file.snapshot`, etc.) | Sensor output | Read/search/fetch/query with native tools |
| `agent_hints` | Sensor author | Follow as the observation contract |

### Hard boundary

Do **not** directly run `runner.py`, restart processes, edit sink files, truncate ring buffers, create missing artifacts, or change lifecycle state unless the user explicitly approves that action.

A `runner.*` file is implementation, not an agent command surface. Treat it like a daemon entrypoint controlled by the user or supervisor. Opening or inspecting `runner.*` can help understand errors, but it does not grant permission to execute it.

## When to Use

Use this skill when:

- A project has `.agents/sensors/**/sensor.yaml`
- You need live logs, runtime state, device signals, event streams, process output, UI logs, DB events, API latency, or other Sensor-backed data
- You see sink types such as `file.ring`, `file.snapshot`, `file.append`, `http.endpoint`, or `db.table`
- You need to interpret `agent_hints.observe_strategy`, `agent_hints.query_examples`, or `agent_hints.freshness_sla`
- A Sensor artifact is missing, stale, malformed, unexpectedly empty, or not updating

Do **not** use this skill for ordinary static project files that are not Sensor artifacts.

## Observation Workflow

1. **Read the manifest first**
   - Locate the relevant `.agents/sensors/<name>/sensor.yaml`.
   - Extract `sink.type`, `sink.target`, `sink.config.format`, `agent_hints.observe_strategy`, `agent_hints.query_examples`, and `agent_hints.freshness_sla`.
   - Resolve relative sink paths from the project root unless the manifest or project convention explicitly says otherwise. If ambiguous, say so before relying on the data.

2. **Observe only through the declared sink**
   - `file.ring`: read/search recent content. Prefer bounded reads with `offset` + `limit` or search for exact terms from `query_examples`.
   - `file.snapshot`: read the whole snapshot if small; otherwise use offsets/limits for large JSON/text snapshots.
   - `file.append`: search or read bounded recent sections; avoid loading unbounded logs.
   - `http.endpoint`: use native fetch tools only if the endpoint is declared and reachable.
   - `db.table`: use native query tools only if already available and authorized.

3. **Check freshness before trusting data**
   - Compare artifact modification time, embedded timestamp, sequence number, heartbeat, or latest event timestamp against `agent_hints.freshness_sla`.
   - If no reliable freshness signal exists, report that freshness cannot be proven.
   - Fresh data may be used as current observation. Stale data is historical evidence only.

4. **Use bounded native reads/searches**
   - Prefer targeted search terms from `agent_hints.query_examples`.
   - Use `offset` + `limit` when reading large artifacts.
   - For ring buffers, inspect recent lines first; do not assume older context exists.
   - For snapshots, treat each read as a point-in-time view and re-read when freshness matters.

5. **Continue work with explicit confidence**
   - State whether your observation is fresh, stale, missing, or unverified.
   - Do not silently base coding decisions on stale Sensor data.

## Feedback Loop with the User

Proactively report through chat when Sensor state affects your work.

### Report stale data

If the artifact age exceeds `freshness_sla`:

> The `<sensor-name>` artifact at `<sink.target>` is stale: latest observed data is `<age>` old, exceeding the `<freshness_sla>` SLA. I will treat it as historical, not current. Please activate or restart `<sensor-name>` if you want me to use live data.

### Report missing or malformed artifacts

If the declared sink target is missing, unreadable, empty when it should not be, or malformed:

> I cannot consume `<sensor-name>` yet because `<sink.target>` is `<missing/unreadable/empty/malformed>`. I will not create or modify the artifact. Please start or repair the Sensor instance, then I can re-read it.

### Escalate unexpected conditions

Stop and notify the user when:

- The manifest and actual artifact disagree
- The sink path is ambiguous or points outside expected project boundaries
- Credentials or secrets appear inline
- Freshness cannot be established but correctness depends on live state
- The Sensor emits errors indicating a resource, permission, or lifecycle problem

### Ask before lifecycle actions

When live data is required and the Sensor is inactive or stale, propose a specific user action instead of doing it silently:

> I recommend activating/restarting Sensor `<sensor-name>` because `<reason>`. The declared lifecycle trigger is `<manual|always_on>` and the runner entrypoint is `<runtime.entrypoint>`. Do you want to start/restart that Sensor now?

Only after explicit approval may you run a documented project command to activate a Sensor. Prefer a project-level command if one exists. Avoid direct `python runner.py` unless the user specifically approves that exact action. If the user says a vague phrase like "make it work" or "fix the sensor," ask a clarifying lifecycle question; do not treat it as permission to execute the runner.

## Quick Reference

| Situation | Do | Do not |
|---|---|---|
| Need current runtime state | Read `sensor.yaml`, then observe `sink.target` | Inspect runtime resource directly by default |
| `file.ring` sink | Read/search recent bounded content | Assume full history exists |
| `file.snapshot` sink | Read snapshot and verify timestamp/mtime | Treat old snapshot as current |
| Artifact stale | Tell user and ask for Sensor activation/restart | Silently run `runner.py` |
| Artifact missing | Report missing sink target | Create the target file yourself |
| Need query strategy | Follow `agent_hints.query_examples` | Invent unrelated probes first |
| Lifecycle issue | Ask user with sensor name and reason | Restart/kill processes unprompted |

## Example

Manifest excerpt:

```yaml
name: ui-logs
runtime:
  entrypoint: runner.py
sink:
  type: file.ring
  target: ./artifacts/ui.ring
agent_hints:
  query_examples:
    - "Search recent output for ERROR"
    - "Read latest 100 lines"
  freshness_sla: "2s"
```

Correct agent behavior:

1. Read `.agents/sensors/ui-logs/sensor.yaml`.
2. Read/search `./artifacts/ui.ring` using bounded native tools.
3. Check mtime or latest event timestamp against `2s`.
4. If fresh, use the latest lines as current UI logs.
5. If stale or missing, tell the user:

> `ui-logs` is not fresh: `./artifacts/ui.ring` is missing or older than the 2s SLA. I will not run `runner.py` directly. Please start/restart the `ui-logs` Sensor, or approve a specific project command for me to do so.

## Common Mistakes

| Mistake | Why it is wrong | Correct behavior |
|---|---|---|
| Running `runner.py` because data is stale | Agent takes lifecycle ownership | Ask user to activate/restart the Sensor |
| Editing the ring/snapshot file | Corrupts the Sensor-owned artifact | Treat sink files as read-only observations |
| Ignoring `freshness_sla` | Uses historical data as if live | Always label fresh/stale/unverified |
| Reading entire large append logs | Wastes context and may miss current data | Use bounded reads/searches and query examples |
| Attaching directly to process/socket/device | Bypasses Sensor abstraction | Consume the declared Observable Artifact |
| Staying silent when live data is unavailable | User cannot fix Sensor state | Report issue and propose next user action |

## Red Flags

Stop and switch to user feedback if you think:

- "I can just run the runner quickly."
- "The ring file is missing, so I will create it."
- "The snapshot is only a little stale; it is probably fine."
- "I should inspect the process/socket directly instead."
- "I can restart the sensor without bothering the user."
- "The manifest says manual, but the user wants speed."

All of these violate the consumer-only model. Read artifacts, report status, and ask before lifecycle changes.
