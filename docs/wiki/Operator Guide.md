# Operator Guide

This guide covers how to configure, run, and operate ProxmoxMCP-Plus.

## Runtime Modes

ProxmoxMCP-Plus can be used in two main ways:

- `MCP stdio mode`: assistants launch the server directly and call tools over MCP
- `MCP Streamable HTTP mode`: the server exposes a native MCP endpoint at `/mcp`
- `OpenAPI mode`: FastAPI wraps the MCP server and exposes HTTP endpoints for other clients

The core tool set is the same in both modes. The difference is only the transport.

## Prerequisites

- Python 3.11 or newer
- Access to a Proxmox VE API endpoint
- A Proxmox API token with the permissions your workflows need
- Network access from the machine running ProxmoxMCP-Plus to the Proxmox API

Optional:

- SSH access from the MCP host to Proxmox nodes if you want LXC command execution tools

## Configuration File

The main config file is `proxmox-config/config.json`.

Start from the example:

```bash
cp proxmox-config/config.example.json proxmox-config/config.json
```

The main sections are:

- `proxmox`: host, port, TLS verification, service type
- `api_tunnel`: optional SSH local forward for the Proxmox API
- `auth`: Proxmox API user and token
- `logging`: log level, format, optional log file
- `mcp`: MCP host, port, transport, and optional transport Host/Origin allowlists
- `security`: currently includes `dev_mode`
- `jobs`: SQLite path for persistent job tracking
- `command_policy`: rules for `execute_*` tools and high-risk mutating operations
- `ssh`: optional SSH settings for LXC command execution

## Runtime Environment Selection

This fork can resolve Proxmox configuration per tool request. Use
`proxmox-config/config.multi-environment.example.json` when one server should
operate across multiple Proxmox environments.

Top-level controls:

- `default_environment`: environment used when a tool call omits `environment`
- `runtime_config_reload`: when true, the config file is reloaded before
  resolving each environment
- `environments`: map of environment name to Proxmox API, auth, optional API
  tunnel, optional SSH settings, and optional job-store path

Every MCP tool schema includes an optional `environment` parameter. OpenAPI
calls generated from the MCP schema also accept `environment` in the request
body. Existing single-environment configs remain valid; the loader normalizes
the root `proxmox`, `auth`, `api_tunnel`, `ssh`, and `jobs` sections into the
default runtime environment.

Example:

```json
{
  "default_environment": "production",
  "runtime_config_reload": true,
  "jobs": {
    "sqlite_path": "proxmox-jobs.sqlite3"
  },
  "environments": {
    "production": {
      "proxmox": {
        "host": "pve-prod.example.internal",
        "port": 8006,
        "verify_ssl": true,
        "service": "PVE"
      },
      "auth": {
        "user": "automation@pve",
        "token_name": "mcp-token",
        "token_value": "prod-token"
      }
    },
    "lab": {
      "proxmox": {
        "host": "pve-lab.example.internal",
        "port": 8006,
        "verify_ssl": true,
        "service": "PVE"
      },
      "auth": {
        "user": "automation@pve",
        "token_name": "mcp-token",
        "token_value": "lab-token"
      },
      "ssh": {
        "user": "mcp-agent",
        "key_file": "~/.ssh/proxmox_lab_key"
      },
      "jobs": {
        "sqlite_path": "proxmox-jobs-lab.sqlite3"
      }
    }
  }
}
```

Operational notes:

- Use environment names that are safe for agents to repeat, such as `production`, `lab`, or `tenant-a`.
- Job state is separated per environment. An environment-specific
  `jobs.sqlite_path` is used directly; otherwise the root job database path is
  suffixed with the environment name.
- SSH-backed container commands resolve SSH settings from the selected
  environment, so each Proxmox environment can use its own node aliases and
  key material.
- The runtime manager caches clients per environment and refreshes them when
  the selected environment config changes.
- This fork is not yet wired into the FortisAI platform deployment.

## Environment Variable Fallback

If `PROXMOX_MCP_CONFIG` is not set or the file is missing, the loader falls back to environment variables.

Common variables:

- `PROXMOX_HOST`
- `PROXMOX_PORT`
- `PROXMOX_USER`
- `PROXMOX_TOKEN_NAME`
- `PROXMOX_TOKEN_VALUE`
- `PROXMOX_VERIFY_SSL`
- `PROXMOX_SERVICE`
- `LOG_LEVEL`
- `MCP_HOST`
- `MCP_PORT`
- `MCP_TRANSPORT`
- `MCP_DNS_REBINDING_PROTECTION`
- `MCP_ALLOWED_HOSTS`
- `MCP_ALLOWED_ORIGINS`
- `PROXMOX_DEV_MODE`
- `COMMAND_POLICY_MODE`
- `PROXMOX_JOBS_SQLITE_PATH`

## Minimal Local Start

```bash
uv venv
uv pip install -e ".[dev]"
$env:PROXMOX_MCP_CONFIG="D:\\PycharmProject\\ProxmoxMCP-Plus\\proxmox-config\\config.json"
python main.py
```

If startup succeeds, the server stays attached to stdio and waits for MCP clients.

## Native MCP Streamable HTTP Mode

Set the MCP transport to `STREAMABLE_HTTP` and bind to an address reachable by the client:

```bash
MCP_HOST=0.0.0.0 MCP_PORT=8000 MCP_TRANSPORT=STREAMABLE_HTTP python -m proxmox_mcp.server
```

The MCP endpoint is:

```text
http://<host>:8000/mcp
```

This is the correct target for MCP clients that support Streamable HTTP. It is separate from the OpenAPI service on port `8811`.

For reverse proxy deployments, configure the external hostnames explicitly instead of disabling DNS rebinding protection:

```bash
MCP_HOST=0.0.0.0 \
MCP_PORT=8000 \
MCP_TRANSPORT=STREAMABLE_HTTP \
MCP_DNS_REBINDING_PROTECTION=true \
MCP_ALLOWED_HOSTS=mcp.example.com:*,localhost:* \
MCP_ALLOWED_ORIGINS=https://mcp.example.com \
python -m proxmox_mcp.server
```

## OpenAPI Mode

You can run the OpenAPI wrapper directly:

```bash
export PROXMOX_API_KEY="$(openssl rand -hex 32)"
python -m proxmox_mcp.openapi_proxy --host 0.0.0.0 --port 8811 -- python main.py
```

OpenAPI mode refuses to start without `PROXMOX_API_KEY` unless
`PROXMOX_ALLOW_NO_AUTH=true` is set for local unauthenticated development.
HTTP clients should send the key as `Authorization: Bearer <PROXMOX_API_KEY>`.

Available routes:

- `/` returns basic service metadata
- `/docs` serves Swagger UI
- `/openapi.json` serves the generated schema
- `/livez` returns minimal unauthenticated process liveness
- `/readyz` returns `503` until the proxy is connected to the MCP backend, then `200`
- `/health` is a readiness alias for `/readyz`
- `/metrics` exposes Prometheus-style request metrics
- `/jobs` exposes direct job query and control routes when a local `JobStore` is available

## Docker Compose Deployment

The repository includes `docker-compose.yml` and `Dockerfile`.

Default Compose behavior:

- Builds the local image
- Mounts `./proxmox-config` read-only into `/app/proxmox-config`
- Exposes `8811`
- Keeps OpenAPI mode as the default Docker runtime
- Sets `PROXMOX_MCP_CONFIG=/app/proxmox-config/config.json`
- Requires `PROXMOX_API_KEY` from your shell or Compose `.env` file
- Adds a container liveness health check against `http://localhost:8811/livez`

Start it with:

```bash
export PROXMOX_API_KEY="${PROXMOX_API_KEY:-$(openssl rand -hex 32)}"
docker compose up -d --build
```

To run the native MCP Streamable HTTP service from Docker Compose:

```bash
docker compose --profile mcp-http up -d proxmox-mcp-http
```

Then connect Streamable HTTP MCP clients to `http://<docker-host>:8000/mcp`.

The same image can also be run directly:

```bash
docker run --rm -p 8000:8000 \
  -e PROXMOX_MCP_MODE=mcp-http \
  -e MCP_HOST=0.0.0.0 \
  -e MCP_PORT=8000 \
  -e MCP_TRANSPORT=STREAMABLE_HTTP \
  -v "$(pwd)/proxmox-config/config.json:/app/proxmox-config/config.json:ro" \
  ghcr.io/rekklesna/proxmoxmcp-plus:latest
```

## Operating Checklist

Before exposing the service to users:

- Confirm the Proxmox API token has only the permissions you intend to expose
- Keep `proxmox.verify_ssl=true` unless you are explicitly in development mode
- Keep `security.dev_mode=false` outside local testing
- Set `PROXMOX_API_KEY` for OpenAPI mode; only use `PROXMOX_ALLOW_NO_AUTH=true` for local unauthenticated development
- For MCP HTTP behind a proxy, keep `MCP_DNS_REBINDING_PROTECTION=true` and set `MCP_ALLOWED_HOSTS` to the exact public hostnames
- Restrict ingress to networks you control
- Monitor `/livez` for process liveness and authenticated `/health` or `/readyz` for backend readiness
- Monitor `/metrics` if you scrape the service with Prometheus-compatible tooling
- Persist the configured `jobs.sqlite_path` on durable storage if job history matters across restarts
- Store logs somewhere persistent if you need auditability

## Command Execution Features

There are two command execution paths:

- `execute_vm_command`: uses QEMU Guest Agent inside VMs
- `execute_container_command`: uses SSH to the Proxmox node and then `pct exec` inside containers

Container command execution is optional per runtime environment. The tools are
registered in the schema, and the selected environment must include an `ssh`
section for LXC command execution to proceed.

For setup details, see [Container Command Execution](Container-Command-Execution).

## Long-Running Job Operations

Asynchronous Proxmox actions now register a persistent job record. This applies to operations such as:

- VM create, start, stop, shutdown, reset, and delete
- container create, start, stop, restart, and delete
- snapshot create, delete, and rollback
- backup create, restore, and delete
- ISO download and delete

Operational guidance:

- Treat `job_id` as the stable identifier you hand back to users, agents, and automation systems.
- Treat `task_id` or `UPID` as Proxmox internals that may change after a retry.
- Keep `jobs.sqlite_path` on a persistent volume in Docker or any long-lived service deployment.
- Use `/jobs/{job_id}/poll` or MCP `poll_job` to refresh progress from Proxmox.
- Use `/jobs/{job_id}/retry` only after reviewing `last_error`, `result`, and `audit_log`.

## First Verification Flow

After deployment, test in this order:

1. Start the service and confirm there are no config validation errors
2. Call read-only tools first: `get_nodes`, `get_vms`, `get_storage`, `get_cluster_status`
3. In OpenAPI mode, confirm `/livez` responds and authenticated `/health` and `/docs` requests work
4. Confirm `/jobs` responds if you expect persistent job tracking
5. If you enabled SSH-backed container commands, confirm the selected environment includes `ssh` settings and that `execute_container_command` can reach a safe test container
6. Only then test mutating tools such as create, start, delete, snapshot, or backup

## Logs and Health

- Application logging is configured under the `logging` section
- `main.py` prints early startup messages to stderr to make bootstrap failures visible
- The OpenAPI wrapper reports `degraded` until it is connected to the MCP subprocess
- authenticated `/health` reports whether direct job routes are enabled in the OpenAPI process

## Related Pages

- [Security Guide](Security-Guide)
- [Container Command Execution](Container-Command-Execution)
- [Integrations Guide](Integrations-Guide)
- [Troubleshooting](Troubleshooting)
