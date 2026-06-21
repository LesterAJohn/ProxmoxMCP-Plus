"""Runtime Proxmox environment resolution.

This module keeps MCP tool registration stable while allowing each tool call to
select a Proxmox environment at request time.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional
from contextvars import ContextVar

from proxmox_mcp.config.loader import load_config
from proxmox_mcp.config.models import Config, EnvironmentConfig, JobsConfig
from proxmox_mcp.core.proxmox import ProxmoxManager
from proxmox_mcp.observability import ToolMetrics
from proxmox_mcp.security import CommandPolicyGate
from proxmox_mcp.services import JobStore
from proxmox_mcp.tools.backup import BackupTools
from proxmox_mcp.tools.cluster import ClusterTools
from proxmox_mcp.tools.containers import ContainerTools
from proxmox_mcp.tools.iso import ISOTools
from proxmox_mcp.tools.jobs import JobsTools
from proxmox_mcp.tools.node import NodeTools
from proxmox_mcp.tools.snapshots import SnapshotTools
from proxmox_mcp.tools.storage import StorageTools
from proxmox_mcp.tools.vm import VMTools


_current_runtime: ContextVar["RuntimeEnvironment | None"] = ContextVar(
    "proxmox_mcp_runtime_environment",
    default=None,
)
_NON_CALLABLE_TOOL_ATTRIBUTES = {"console_manager"}


def _fingerprint(config: Config, environment: str, env_config: EnvironmentConfig, sqlite_path: str) -> str:
    payload = {
        "environment": environment,
        "config": env_config.model_dump(mode="json"),
        "command_policy": config.command_policy.model_dump(mode="json"),
        "jobs_sqlite_path": sqlite_path,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _environment_sqlite_path(config: Config, environment: str, env_config: EnvironmentConfig) -> str:
    if env_config.jobs is not None:
        return str(Path(env_config.jobs.sqlite_path).expanduser())
    jobs_config: JobsConfig = config.jobs
    sqlite_path = Path(jobs_config.sqlite_path).expanduser()
    if len(config.environments) <= 1:
        return str(sqlite_path)
    suffix = sqlite_path.suffix or ".sqlite3"
    return str(sqlite_path.with_name(f"{sqlite_path.stem}-{environment}{suffix}"))


@dataclass
class RuntimeEnvironment:
    """Fully resolved per-environment runtime dependencies."""

    name: str
    fingerprint: str
    config: Config
    environment_config: EnvironmentConfig
    proxmox_manager: ProxmoxManager
    proxmox: Any
    command_policy: CommandPolicyGate
    job_store: JobStore
    node_tools: NodeTools
    vm_tools: VMTools
    storage_tools: StorageTools
    cluster_tools: ClusterTools
    container_tools: ContainerTools
    snapshot_tools: SnapshotTools
    iso_tools: ISOTools
    backup_tools: BackupTools
    jobs_tools: JobsTools

    def close(self) -> None:
        self.job_store.close()
        self.proxmox_manager.close()


class RuntimeEnvironmentManager:
    """Build and cache request-scoped Proxmox runtime dependencies."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        initial_config: Config,
        metrics: ToolMetrics,
        logger: logging.Logger,
    ) -> None:
        self.config_path = config_path
        self._initial_config = initial_config
        self.metrics = metrics
        self.logger = logger
        self._cache: dict[str, RuntimeEnvironment] = {}

    def _load_current_config(self) -> Config:
        if self.config_path and self._initial_config.runtime_config_reload:
            return load_config(self.config_path)
        return self._initial_config

    def _resolve_environment_name(self, config: Config, environment: Optional[str]) -> str:
        selected = (environment or config.default_environment or "default").strip()
        if not selected:
            selected = "default"
        if selected not in config.environments:
            available = ", ".join(sorted(config.environments)) or "none"
            raise ValueError(f"Unknown Proxmox environment '{selected}'. Available environments: {available}")
        return selected

    def get_runtime(self, environment: Optional[str] = None) -> RuntimeEnvironment:
        config = self._load_current_config()
        selected = self._resolve_environment_name(config, environment)
        env_config = config.environments[selected]
        sqlite_path = _environment_sqlite_path(config, selected, env_config)
        fingerprint = _fingerprint(config, selected, env_config, sqlite_path)
        cached = self._cache.get(selected)
        if cached is not None and cached.fingerprint == fingerprint:
            return cached
        if cached is not None:
            cached.close()

        proxmox_manager = ProxmoxManager(
            env_config.proxmox,
            env_config.auth,
            api_tunnel_config=env_config.api_tunnel,
            ssh_config=env_config.ssh,
        )
        proxmox = proxmox_manager.get_api()
        command_policy = CommandPolicyGate(config.command_policy)
        job_store = JobStore(proxmox, sqlite_path=sqlite_path)
        runtime = RuntimeEnvironment(
            name=selected,
            fingerprint=fingerprint,
            config=config,
            environment_config=env_config,
            proxmox_manager=proxmox_manager,
            proxmox=proxmox,
            command_policy=command_policy,
            job_store=job_store,
            node_tools=NodeTools(proxmox, metrics=self.metrics, job_store=job_store),
            vm_tools=VMTools(
                proxmox,
                command_policy=command_policy,
                metrics=self.metrics,
                job_store=job_store,
            ),
            storage_tools=StorageTools(proxmox, metrics=self.metrics, job_store=job_store),
            cluster_tools=ClusterTools(proxmox, metrics=self.metrics, job_store=job_store),
            container_tools=ContainerTools(
                proxmox,
                env_config.ssh,
                command_policy=command_policy,
                metrics=self.metrics,
                job_store=job_store,
            ),
            snapshot_tools=SnapshotTools(proxmox, metrics=self.metrics, job_store=job_store),
            iso_tools=ISOTools(proxmox, metrics=self.metrics, job_store=job_store),
            backup_tools=BackupTools(proxmox, metrics=self.metrics, job_store=job_store),
            jobs_tools=JobsTools(job_store),
        )
        self._cache[selected] = runtime
        self.logger.info("Resolved Proxmox runtime environment '%s'", selected)
        return runtime

    @contextlib.contextmanager
    def use(self, environment: Optional[str] = None) -> Iterator[RuntimeEnvironment]:
        runtime = self.get_runtime(environment)
        token = _current_runtime.set(runtime)
        try:
            yield runtime
        finally:
            _current_runtime.reset(token)

    def current_runtime(self) -> RuntimeEnvironment:
        runtime = _current_runtime.get()
        if runtime is not None:
            return runtime
        return self.get_runtime(None)

    def close(self) -> None:
        for runtime in list(self._cache.values()):
            runtime.close()
        self._cache.clear()


class RuntimeToolProxy:
    """Expose the selected runtime's tool object through the original server API."""

    def __init__(self, manager: RuntimeEnvironmentManager, attribute: str) -> None:
        self.manager = manager
        self.attribute = attribute

    def __getattr__(self, name: str) -> Any:
        if name in _NON_CALLABLE_TOOL_ATTRIBUTES:
            target = getattr(self.manager.current_runtime(), self.attribute)
            return getattr(target, name)

        def resolved(*args: Any, **kwargs: Any) -> Any:
            target = getattr(self.manager.current_runtime(), self.attribute)
            return getattr(target, name)(*args, **kwargs)

        return resolved


class RuntimeCommandPolicyProxy:
    """Expose request-scoped command policy decisions."""

    def __init__(self, manager: RuntimeEnvironmentManager) -> None:
        self.manager = manager

    def evaluate_operation(self, *args: Any, **kwargs: Any) -> Any:
        return self.manager.current_runtime().command_policy.evaluate_operation(*args, **kwargs)

    def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        return self.manager.current_runtime().command_policy.evaluate(*args, **kwargs)


class RuntimeJobStoreProxy:
    """Expose the selected runtime's job store through the original server API."""

    def __init__(self, manager: RuntimeEnvironmentManager) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self.manager.current_runtime().job_store, name)
