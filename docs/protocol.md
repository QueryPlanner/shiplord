# Shiplord protocol (version 1)

Two entry points on the server speak this protocol over SSH. Both read one request and write one response.

| Entry point | Reached by | Purpose |
| --- | --- | --- |
| helper (`shiplord-host`) | an application account's forced command, then sudo | application operations |
| admin (`shiplord-admin`) | an administrator's own SSH login and sudo | bootstrap-era and administration operations |

The dispatcher (`shiplord-dispatch`) with no argument is an unprivileged, capabilities-only probe used by
`doctor --server`; it grants no application access.

## Framing

Each message is a four-byte unsigned big-endian length followed by that many bytes of UTF-8 JSON. The length is
between 1 and 1,048,576 bytes, for requests and responses alike. The JSON value must be an object; duplicate keys,
non-finite numbers, malformed UTF-8, and truncated frames are invalid. A reader consumes exactly one frame.

The client sends one request frame and then closes stdin (the only exception is a native upload, below). The
server writes one response frame and exits. Standard output carries only the response; diagnostics never go there.

## Request envelope

```json
{"version": 1, "request_id": "deploy-ab12…", "action": "deploy",
 "application_id": "orders", "commit": "<40 hex>", "arguments": {}}
```

`request_id` and `application_id` match `[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}`. `commit` is null or a full commit SHA.
All fields are required and unknown fields are rejected. The version is checked on every request before any
action, so an incompatible peer fails before any mutation:
`{"status": "incompatible", "request_id": …, "supported_versions": [1]}` with exit status 4.

## Helper actions

The application is established by the sudo caller's registration, never by the request: a request naming another
application is denied (`{"status": "error", "error": "access_denied"}`, exit 3).

| Action | `commit` | `arguments` | Result |
| --- | --- | --- | --- |
| `capabilities` | null | none | `status: ok`, `version`, `helper_version`, `actions` |
| `status` | null | `job_id` (optional) | `result.application` (current, previous, running, unfinished transaction), `result.job` |
| `logs` | null | `tail` 1–1000 | `result.logs` (bounded) |
| `plan` | required | `sequence`, `artifact`, `configuration`, `allow_older` | `result`: what a deploy would do; read-only |
| `deploy` | required | `sequence`, `artifact`, `configuration`, `allow_older` | `accepted` with a job, or `finished` with an outcome |
| `rollback` | null | none | `accepted` with a job |
| `recover` | null | none | `accepted` with a job |

`capabilities` lists optional features after the actions in the same `actions` array (an older client rejects unknown
response fields but accepts any extra name). `docker_storage_v1` means the helper honours `[storage]` in a Docker
`configuration`; it validates `storage.container_path` itself, independent of the client. `plan` adds
`storage: {current_path, candidate_path, same_volume, note}` for Docker applications.

`docker_shutdown_v1` means the helper honours `deploy.shutdown_timeout_seconds` / `shutdown_timeout_action`. `plan` adds
`shutdown: {outgoing_policy, candidate_policy, note}`, and a finished deploy may include `shutdown` stage reports
(`stop_requested`, `signalled`, `waiting`, `exited`, `forced`, `deadline_expired`).

Arguments are validated before any host state is opened. `artifact` is `{"kind": "docker", "image":
"registry/name@sha256:<64 hex>"}` (the repository must equal the registered one) or `{"kind": "native", "sha256",
"size", "architecture"}`. `configuration` is a complete `ship.toml` as JSON; its hostname must equal the registered
one. `sequence` is the release ordering key (the commit's committer timestamp).

### Jobs

`deploy`, `rollback`, and `recover` create a job record keyed by `request_id` and start a worker as a transient
systemd unit, so the operation survives the SSH session. A repeated `request_id` returns the recorded job whatever
its outcome was; changed content under the same ID is refused. The response is:

```json
{"status": "accepted", "request_id": "…", "job": {"state": "queued|running|finished|interrupted", "phase": "…",
 "submitted_at": 0, "started_at": null, "finished_at": null, "outcome": null}}
```

Poll `status` with `job_id` until `state` is `finished` (then `outcome` is set) or `interrupted` (the worker
vanished; run `recover`). A `deploy` refused by the immediate checks returns `{"status": "finished", "outcome":
…}` without creating a job. An outcome is `{"status", "exit_code", "detail"}`:

| `status` | `exit_code` |
| --- | --- |
| `succeeded`, `unchanged`, `already_applied`, `recovered`, `nothing_to_recover` | 0 |
| `failed`, `stale_release` (error in `detail`) | 1 |
| `rejected` | 2 |
| `rolled_back` | 5 |
| `intervention_required` | 6 |

### Native upload

A `deploy` whose artifact is `native` is followed on the same stream by exactly `size` raw bytes. The helper streams
them into a root-only staging file, verifies the exact length, the SHA-256, and a 64-bit little-endian ELF header
for a supported architecture matching the server, and only then publishes it under its checksum. A repeated or
refused request has its upload drained and discarded. No other request may send bytes after its frame. The
helper's 15-second overall deadline is extended to `30 s + size / 128 KiB/s` (at most one hour) for an upload.

## Admin actions

The admin entry point reads one frame from stdin (after sudo's password line, if one was needed) and writes one
frame. Nothing travels in argv. Requests carry `application_id`, null `commit`, and these actions:

| Action | `arguments` |
| --- | --- |
| `provision` | `runtime`, `api_domain`, `image_repository` (null for native), `public_key`, `key_label` |
| `keys` | `operation` (`add`, `remove`, `list`), `label`, `public_key` |
| `environment` | `operation` (`set`, `clear`, `keys`), `text` |
| `registry` | `operation` (`login`, `logout`, `list`), `registry` (`ghcr.io`), `username`, `token` |
| `doctor` | none; names no application (`application_id` is null) |
| `deprovision` | optional `retire`: `true` (stop, unroute and discard releases first) |

Responses are `{"status": "ok", "request_id", "result"}` or `{"status": "error", "request_id", "error":
"denied|invalid_request|failed", "message"}`. Provisioning is idempotent and writes the registration last, because
the registration is what enables an application's access.

## Exit status of the entry points

0 success; 1 failure; 2 invalid request; 3 denied; 4 incompatible version. The CLI maps results to its own exit
codes (see the README).
