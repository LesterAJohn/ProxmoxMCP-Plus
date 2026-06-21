"""
Main server implementation for Proxmox MCP.

This module wires configuration, Proxmox connectivity, observability, policy
controls, and pluggable MCP tool registration together.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import Any, Literal, Optional, cast

from mcp.server.fastmcp import FastMCP
from proxmox_mcp.config.loader import load_config
from proxmox_mcp.core.logging import setup_logging
from proxmox_mcp.observability import ToolMetrics
from proxmox_mcp.runtime import (
    RuntimeCommandPolicyProxy,
    RuntimeEnvironmentManager,
    RuntimeJobStoreProxy,
    RuntimeToolProxy,
)
from proxmox_mcp.services import ToolRegistry
from proxmox_mcp.services.builtin_tool_plugins import (
    BackupToolsPlugin,
    ContainerToolsPlugin,
    CoreToolsPlugin,
    ImageToolsPlugin,
    JobsToolsPlugin,
    SnapshotToolsPlugin,
    VMToolsPlugin,
)

TransportSecuritySettings: Any
try:
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:  # pragma: no cover - exercised only with older MCP SDKs
    TransportSecuritySettings = None


def _log_safe(value: object, max_length: int = 200) -> str:
    text = str(value).replace("\r", "").replace("\n", "")
    return text[:max_length]


class ProxmoxMCPServer:
    """Main server class for Proxmox MCP."""

    def __init__(self, config_path: Optional[str] = None):
        self.config = load_config(config_path)
        self.logger = setup_logging(self.config.logging)
        self.metrics = ToolMetrics()
        self.runtime_manager = RuntimeEnvironmentManager(
            config_path=config_path,
            initial_config=self.config,
            metrics=self.metrics,
            logger=self.logger,
        )
        self.proxmox_manager = RuntimeToolProxy(self.runtime_manager, "proxmox_manager")
        self.proxmox = RuntimeToolProxy(self.runtime_manager, "proxmox")
        self.command_policy = RuntimeCommandPolicyProxy(self.runtime_manager)
        self.job_store = RuntimeJobStoreProxy(self.runtime_manager)
        self.node_tools = RuntimeToolProxy(self.runtime_manager, "node_tools")
        self.vm_tools = RuntimeToolProxy(self.runtime_manager, "vm_tools")
        self.storage_tools = RuntimeToolProxy(self.runtime_manager, "storage_tools")
        self.cluster_tools = RuntimeToolProxy(self.runtime_manager, "cluster_tools")
        self.container_tools = RuntimeToolProxy(self.runtime_manager, "container_tools")
        self.snapshot_tools = RuntimeToolProxy(self.runtime_manager, "snapshot_tools")
        self.iso_tools = RuntimeToolProxy(self.runtime_manager, "iso_tools")
        self.backup_tools = RuntimeToolProxy(self.runtime_manager, "backup_tools")
        self.jobs_tools = RuntimeToolProxy(self.runtime_manager, "jobs_tools")

        log_level = cast(
            Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            self.config.logging.level.upper(),
        )
        transport_security = self._build_transport_security()
        if transport_security is None:
            self.mcp = FastMCP(
                "ProxmoxMCP",
                host=self.config.mcp.host,
                port=self.config.mcp.port,
                log_level=log_level,
            )
        else:
            self.mcp = FastMCP(
                "ProxmoxMCP",
                host=self.config.mcp.host,
                port=self.config.mcp.port,
                log_level=log_level,
                transport_security=transport_security,
            )
        self.tool_registry = ToolRegistry()
        self._setup_tools()

    def _build_transport_security(self) -> Any | None:
        mcp_config = self.config.mcp
        configured = (
            mcp_config.dns_rebinding_protection is not None
            or bool(mcp_config.allowed_hosts)
            or bool(mcp_config.allowed_origins)
        )
        if not configured:
            return None
        if TransportSecuritySettings is None:
            raise RuntimeError(
                "MCP transport security settings require mcp>=1.24.0. "
                "Upgrade the mcp package or remove mcp.dns_rebinding_protection, "
                "mcp.allowed_hosts, and mcp.allowed_origins from the config."
            )

        enable_protection = mcp_config.dns_rebinding_protection
        if enable_protection is None:
            enable_protection = True

        return TransportSecuritySettings(
            enable_dns_rebinding_protection=enable_protection,
            allowed_hosts=mcp_config.allowed_hosts,
            allowed_origins=mcp_config.allowed_origins,
        )

    def _setup_tools(self) -> None:
        self.tool_registry.add(CoreToolsPlugin())
        self.tool_registry.add(JobsToolsPlugin())
        self.tool_registry.add(VMToolsPlugin())
        self.tool_registry.add(ContainerToolsPlugin())
        self.tool_registry.add(SnapshotToolsPlugin())
        self.tool_registry.add(ImageToolsPlugin())
        self.tool_registry.add(BackupToolsPlugin())
        self.tool_registry.register_all(self)

    def close(self) -> None:
        self.runtime_manager.close()

    def start(self) -> None:
        """Start the MCP server with the configured transport."""
        import anyio

        def signal_handler(signum: int, frame: object) -> None:
            self.logger.info("Received signal to shutdown...")
            self.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            transport = self.config.mcp.transport
            self.logger.info("Starting Proxmox MCP Server with transport: %s", transport)

            if transport == "STDIO":
                anyio.run(self.mcp.run_stdio_async)
            elif transport == "SSE":
                anyio.run(self.mcp.run_sse_async)
            elif transport == "STREAMABLE":
                try:
                    anyio.run(self.mcp.run_streamable_http_async)
                except AttributeError:
                    anyio.run(self.mcp.run_sse_async)
            else:
                anyio.run(self.mcp.run_stdio_async)
        except Exception as e:
            self.logger.error("Server execution failed: %s", _log_safe(e))
            sys.exit(1)
        finally:
            self.close()


def main() -> None:
    """CLI entrypoint for running the Proxmox MCP server."""
    config_path = os.getenv("PROXMOX_MCP_CONFIG")

    try:
        server = ProxmoxMCPServer(config_path)
        server.start()
    except KeyboardInterrupt:
        print("\nShutting down gracefully...", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        print(f"Server initialization failed: {e}", file=sys.stderr)
        sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
