"""RuntimeMachine and RuntimeCapability registration tests."""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ariadne.api import app
from ariadne.backends import get_backend
from ariadne.cli import app as cli_app
from ariadne.daemon import Daemon
from ariadne import models
from ariadne.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_runtime_machine_and_capability_registration_is_idempotent(store: Store):
    assert hasattr(models, "RuntimeMachineStatus")
    assert hasattr(models, "RuntimeCapabilityStatus")
    RuntimeMachineStatus = models.RuntimeMachineStatus
    RuntimeCapabilityStatus = models.RuntimeCapabilityStatus

    machine = store.register_runtime_machine(
        runtime_machine_id="rt-local",
        name="Local Runtime",
        version="0.1.0",
        workspace_root="/repo",
        max_concurrent_taskruns=2,
        repo_allowlist=["/repo"],
        device_info={"hostname": "devbox"},
    )

    assert machine.id == "rt-local"
    assert machine.status == RuntimeMachineStatus.ONLINE
    assert machine.workspace_root == "/repo"
    assert machine.repo_allowlist == ["/repo"]
    assert machine.device_info["hostname"] == "devbox"

    updated = store.register_runtime_machine(
        runtime_machine_id="rt-local",
        name="Local Runtime",
        version="0.1.1",
        workspace_root="/repo",
        max_concurrent_taskruns=4,
        repo_allowlist=["/repo", "/other"],
        device_info={"hostname": "devbox"},
    )

    assert updated.id == machine.id
    assert updated.version == "0.1.1"
    assert updated.max_concurrent_taskruns == 4
    assert updated.repo_allowlist == ["/repo", "/other"]
    assert len(store.list_runtime_machines()) == 1

    heartbeat = store.heartbeat_runtime_machine("rt-local")
    assert heartbeat.last_heartbeat_at is not None
    assert heartbeat.status == RuntimeMachineStatus.ONLINE

    dry_run = store.upsert_runtime_capability(
        runtime_machine_id="rt-local",
        provider="dry-run",
        command_path="dry-run",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )
    codex = store.upsert_runtime_capability(
        runtime_machine_id="rt-local",
        provider="codex",
        command_path="codex",
        status=RuntimeCapabilityStatus.UNAVAILABLE,
        health_error="codex not found",
    )

    assert dry_run.status == RuntimeCapabilityStatus.AVAILABLE
    assert codex.status == RuntimeCapabilityStatus.UNAVAILABLE
    assert codex.health_error == "codex not found"
    assert len(store.list_runtime_capabilities("rt-local")) == 2

    disabled = store.set_runtime_capability_status(
        dry_run.id,
        RuntimeCapabilityStatus.DISABLED,
        health_error="operator disabled",
    )
    assert disabled.status == RuntimeCapabilityStatus.DISABLED
    assert disabled.health_error == "operator disabled"


def test_daemon_start_registers_runtime_machine_and_capabilities(store: Store, tmp_path):
    assert hasattr(models, "RuntimeMachineStatus")
    assert hasattr(models, "RuntimeCapabilityStatus")
    RuntimeMachineStatus = models.RuntimeMachineStatus
    RuntimeCapabilityStatus = models.RuntimeCapabilityStatus

    daemon = Daemon(
        store=store,
        backend_factory=get_backend,
        runtime_id="rt-test",
        poll_interval=0.01,
        target_repo_path=str(tmp_path),
    )

    daemon.start(max_iterations=1)

    machine = store.get_runtime_machine("rt-test")
    assert machine is not None
    assert machine.status == RuntimeMachineStatus.ONLINE
    assert machine.workspace_root == str(tmp_path)
    assert machine.last_heartbeat_at is not None

    caps = {c.provider: c for c in store.list_runtime_capabilities("rt-test")}
    assert "dry-run" in caps
    assert caps["dry-run"].status == RuntimeCapabilityStatus.AVAILABLE
    assert "codex" in caps
    assert "claude-code" in caps
    assert caps["codex"].status in {
        RuntimeCapabilityStatus.AVAILABLE,
        RuntimeCapabilityStatus.UNAVAILABLE,
    }


def test_runtime_cli_and_api_surfaces(tmp_path, monkeypatch):
    assert hasattr(models, "RuntimeCapabilityStatus")
    RuntimeCapabilityStatus = models.RuntimeCapabilityStatus

    db = str(tmp_path / "runtime.db")
    monkeypatch.setattr("ariadne.cli._db_path", db)
    monkeypatch.setattr("ariadne.api._db_path", db)
    store = Store(db)
    store.register_runtime_machine(
        runtime_machine_id="rt-cli",
        name="CLI Runtime",
        version="0.1.0",
        workspace_root=str(tmp_path),
    )
    store.upsert_runtime_capability(
        runtime_machine_id="rt-cli",
        provider="dry-run",
        command_path="dry-run",
        status=RuntimeCapabilityStatus.AVAILABLE,
    )
    store.close()

    runner = CliRunner()
    result = runner.invoke(cli_app, ["runtime-list"])
    assert result.exit_code == 0
    assert "rt-cli" in result.stdout
    assert "online" in result.stdout

    result = runner.invoke(cli_app, ["capability-list"])
    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert "available" in result.stdout

    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert "RuntimeMachines" in client.get("/").text
    assert "RuntimeCapabilities" in client.get("/").text

    machines = client.get("/api/runtime-machines")
    assert machines.status_code == 200
    assert machines.json()[0]["id"] == "rt-cli"

    capabilities = client.get("/api/runtime-capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()[0]["provider"] == "dry-run"
