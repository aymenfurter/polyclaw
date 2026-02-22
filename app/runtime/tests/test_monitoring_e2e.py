"""End-to-end monitoring test -- provision App Insights, send OTel
telemetry, query Log Analytics, and tear down.

Provisions an Application Insights resource backed by a Log Analytics
workspace, configures the ``azure-monitor-opentelemetry`` distro to
export telemetry, creates custom spans and log records, waits for
ingestion, queries the workspace via ``az monitor log-analytics query``,
verifies the data arrived, then deletes all resources.

Usage
-----
    python -m pytest app/runtime/tests/test_monitoring_e2e.py \
        -v --tb=short --run-slow -s

Requires:
    - ``az`` CLI installed and logged in (``az login``)
    - An active Azure subscription
    - ``pip install azure-monitor-opentelemetry opentelemetry-api opentelemetry-sdk``
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid

import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_UNIQUE = uuid.uuid4().hex[:8]
RESOURCE_GROUP = f"otel-test-{_UNIQUE}-rg"
APP_INSIGHTS_NAME = f"otel-test-{_UNIQUE}-ai"
WORKSPACE_NAME = f"otel-test-{_UNIQUE}-ws"
LOCATION = "eastus"
TIMEOUT_PROVISION = 300  # seconds
TIMEOUT_QUERY = 60

# Marker that we embed in spans so we can query for it.
TEST_MARKER = f"polyclaw-otel-e2e-{_UNIQUE}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _az(
    *args: str, check: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Run an ``az`` CLI command and return the result."""
    cmd = ["az", *args, "--output", "json"]
    logger.info("[az] %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"az command failed (rc={result.returncode}):\n{result.stderr}"
        )
    return result


def _az_json(*args: str, **kwargs) -> dict | list | None:
    """Run ``az`` and parse JSON output."""
    result = _az(*args, **kwargs)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Resource lifecycle
# ---------------------------------------------------------------------------


def _ensure_extension() -> None:
    """Ensure the ``application-insights`` CLI extension is installed."""
    _az("extension", "add", "--name", "application-insights", "--yes", check=False)


def _create_resource_group() -> None:
    logger.info("Creating resource group %s in %s ...", RESOURCE_GROUP, LOCATION)
    _az("group", "create", "--name", RESOURCE_GROUP, "--location", LOCATION)


def _create_workspace() -> str:
    """Create a Log Analytics workspace. Returns the workspace resource ID."""
    logger.info("Creating Log Analytics workspace %s ...", WORKSPACE_NAME)
    result = _az_json(
        "monitor",
        "log-analytics",
        "workspace",
        "create",
        "--workspace-name",
        WORKSPACE_NAME,
        "--resource-group",
        RESOURCE_GROUP,
        "--location",
        LOCATION,
        timeout=TIMEOUT_PROVISION,
    )
    assert result and isinstance(result, dict), "Failed to create workspace"
    ws_id: str = result["id"]
    logger.info("Workspace created: %s", ws_id)
    return ws_id


def _create_app_insights(ws_id: str) -> str:
    """Create Application Insights linked to the workspace. Returns the connection string."""
    logger.info("Creating Application Insights %s ...", APP_INSIGHTS_NAME)
    result = _az_json(
        "monitor",
        "app-insights",
        "component",
        "create",
        "--app",
        APP_INSIGHTS_NAME,
        "--location",
        LOCATION,
        "--resource-group",
        RESOURCE_GROUP,
        "--workspace",
        ws_id,
        "--application-type",
        "web",
        timeout=TIMEOUT_PROVISION,
    )
    assert result and isinstance(result, dict), "Failed to create App Insights"
    cs: str = result.get("connectionString", "")
    assert cs, "Connection string not found in response"
    logger.info("App Insights created -- connection string obtained")
    return cs


def _get_workspace_id() -> str:
    """Return the Log Analytics workspace ``customerId`` (GUID) for querying."""
    result = _az_json(
        "monitor",
        "log-analytics",
        "workspace",
        "show",
        "--workspace-name",
        WORKSPACE_NAME,
        "--resource-group",
        RESOURCE_GROUP,
    )
    assert result and isinstance(result, dict)
    customer_id: str = result["customerId"]
    logger.info("Workspace customerId: %s", customer_id)
    return customer_id


def _delete_resource_group() -> None:
    logger.info("Deleting resource group %s ...", RESOURCE_GROUP)
    _az(
        "group",
        "delete",
        "--name",
        RESOURCE_GROUP,
        "--yes",
        "--no-wait",
        check=False,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# OTel helpers
# ---------------------------------------------------------------------------


def _configure_and_send_telemetry(connection_string: str) -> None:
    """Configure the Azure Monitor distro and emit test spans + logs."""
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=connection_string,
        sampling_ratio=1.0,
        enable_live_metrics=False,
    )

    from opentelemetry import trace

    tracer = trace.get_tracer("polyclaw.test")

    # Create a parent span with the test marker.
    with tracer.start_as_current_span(
        "test.e2e_parent",
        attributes={"test.marker": TEST_MARKER, "test.suite": "monitoring_e2e"},
    ) as parent:
        parent.add_event("test_event", attributes={"detail": "e2e event"})

        # Nested child span.
        with tracer.start_as_current_span(
            "test.e2e_child",
            attributes={"test.marker": TEST_MARKER, "test.step": "child"},
        ):
            pass

    # Also emit a log record through Python logging so it's captured
    # by the OTel log handler.
    logging.getLogger("polyclaw.test").warning(
        "E2E monitoring test log -- marker=%s", TEST_MARKER
    )

    # Flush everything.
    tp = trace.get_tracer_provider()
    if hasattr(tp, "force_flush"):
        tp.force_flush(timeout_millis=30_000)

    from opentelemetry._logs import get_logger_provider

    lp = get_logger_provider()
    if hasattr(lp, "force_flush"):
        lp.force_flush(timeout_millis=30_000)

    logger.info("[otel] Telemetry flushed -- marker=%s", TEST_MARKER)


def _shutdown_otel() -> None:
    """Shut down all OTel providers to release resources."""
    try:
        from opentelemetry import trace, metrics
        from opentelemetry._logs import get_logger_provider

        tp = trace.get_tracer_provider()
        if hasattr(tp, "shutdown"):
            tp.shutdown()

        mp = metrics.get_meter_provider()
        if hasattr(mp, "shutdown"):
            mp.shutdown()

        lp = get_logger_provider()
        if hasattr(lp, "shutdown"):
            lp.shutdown()

        logger.info("[otel] Providers shut down")
    except Exception:
        logger.warning("[otel] Error during shutdown", exc_info=True)


# ---------------------------------------------------------------------------
# Log Analytics query helpers
# ---------------------------------------------------------------------------


def _query_log_analytics(workspace_customer_id: str, kql: str) -> list[dict]:
    """Execute a KQL query against the Log Analytics workspace via az CLI."""
    result = _az(
        "monitor",
        "log-analytics",
        "query",
        "--workspace",
        workspace_customer_id,
        "--analytics-query",
        kql,
        timeout=TIMEOUT_QUERY,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("[query] Query failed: %s", result.stderr[:500])
        return []
    try:
        data = json.loads(result.stdout)
        if isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError:
        return []


def _wait_for_telemetry(
    workspace_customer_id: str,
    *,
    max_wait: int = 360,
    poll_interval: int = 30,
) -> list[dict]:
    """Poll Log Analytics until the test marker appears in AppTraces or AppDependencies.

    Application Insights ingestion typically takes 2-5 minutes.
    """
    # Query both traces (custom spans appear as dependencies or requests)
    # and AppTraces (log records).
    kql = (
        f'AppDependencies | where Properties["test.marker"] == "{TEST_MARKER}" '
        f'| project OperationName=Name, Type="dependency", Marker=Properties["test.marker"] '
        f"| union ("
        f'AppTraces | where Message contains "{TEST_MARKER}" '
        f'| project OperationName=OperationName, Type="trace", Marker=Message'
        f") | take 10"
    )

    deadline = time.time() + max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        logger.info(
            "[query] Polling for telemetry (attempt %d, %.0fs remaining) ...",
            attempt,
            deadline - time.time(),
        )
        rows = _query_log_analytics(workspace_customer_id, kql)
        if rows:
            logger.info("[query] Found %d rows", len(rows))
            return rows
        time.sleep(poll_interval)

    return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestMonitoringE2E:
    """End-to-end OpenTelemetry monitoring viability check.

    Run with ``--run-slow`` to execute (skipped by default).
    All resources are created with a random suffix and deleted in teardown.

    Steps:
        1. Provision App Insights + Log Analytics via ``az`` CLI
        2. Retrieve the connection string
        3. Configure the Azure Monitor OTel distro and emit test telemetry
        4. Query Log Analytics to verify data arrived
        5. Tear down all resources
    """

    _connection_string: str = ""
    _workspace_customer_id: str = ""

    # -- fixtures -----------------------------------------------------------

    @pytest.fixture(autouse=True, scope="class")
    def azure_resources(self, request: pytest.FixtureRequest):
        """Provision Azure monitoring resources before the test class,
        delete after."""
        _ensure_extension()
        _create_resource_group()

        try:
            ws_id = _create_workspace()
            cs = _create_app_insights(ws_id)
            customer_id = _get_workspace_id()

            request.cls._connection_string = cs
            request.cls._workspace_customer_id = customer_id

            logger.info(
                "Provisioned: app_insights=%s workspace=%s",
                APP_INSIGHTS_NAME,
                WORKSPACE_NAME,
            )

            yield

        finally:
            _shutdown_otel()
            _delete_resource_group()

    # -- step 1: validate connection string --------------------------------

    def test_01_connection_string_format(self) -> None:
        """Verify the provisioned connection string has the expected format."""
        cs = self._connection_string
        assert cs, "Connection string is empty"
        parts: dict[str, str] = {}
        for segment in cs.split(";"):
            if "=" in segment:
                key, _, value = segment.partition("=")
                parts[key.strip()] = value.strip()

        assert "InstrumentationKey" in parts, (
            f"Missing InstrumentationKey in: {cs[:80]}..."
        )
        assert "IngestionEndpoint" in parts, (
            f"Missing IngestionEndpoint in: {cs[:80]}..."
        )
        logger.info(
            "[step1] Connection string valid -- ikey=%s...",
            parts["InstrumentationKey"][:8],
        )

    # -- step 2: configure OTel and send telemetry -------------------------

    def test_02_send_telemetry(self) -> None:
        """Configure the Azure Monitor distro, emit custom spans + logs,
        and flush the exporters."""
        _configure_and_send_telemetry(self._connection_string)
        # If we get here without exception, OTel was initialised successfully.

    # -- step 3: query Log Analytics to verify arrival ---------------------

    def test_03_query_telemetry(self) -> None:
        """Wait for telemetry to appear in Log Analytics and verify it."""
        rows = _wait_for_telemetry(self._workspace_customer_id)
        assert rows, (
            f"Telemetry with marker {TEST_MARKER} did not appear in "
            f"Log Analytics within the polling window."
        )
        logger.info("[step3] Verified %d telemetry rows in Log Analytics", len(rows))
        for row in rows:
            logger.info("  %s", row)
