"""Azure BYOK viability test -- end-to-end Foundry + Copilot SDK BYOK.

Provisions an Azure AI Services (Foundry) resource, deploys a model,
verifies inference directly and through the Copilot SDK BYOK provider,
then tears everything down.

Uses Entra ID (bearer token) auth -- most Azure subscriptions have a
policy that disables key-based auth on Cognitive Services resources.
The Copilot SDK supports this via the ``bearer_token`` field in
``ProviderConfig``.

Usage
-----
    python -m pytest app/runtime/tests/test_azure_byok_viability.py \
        -v --tb=short --run-slow -s

Requires:
    - ``az`` CLI installed and logged in (``az login``)
    - An active Azure subscription
    - ``pip install openai azure-identity`` (already in deps)
    - The ``github-copilot-sdk`` package (already in deps)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid

import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_UNIQUE = uuid.uuid4().hex[:8]
RESOURCE_GROUP = f"byok-test-{_UNIQUE}-rg"
RESOURCE_NAME = f"byok-test-{_UNIQUE}"
LOCATION = "eastus2"
MODEL_NAME = "gpt-4.1-nano"
MODEL_VERSION = "2025-04-14"
DEPLOYMENT_NAME = f"byok-{_UNIQUE}"
SKU_NAME = "GlobalStandard"
SKU_CAPACITY = 10  # Must be >= 10; SDK adds ~4K token system prompt, which
                   # breaches the 1K TPM limit of capacity-1 deployments.
TIMEOUT_PROVISION = 300  # seconds
TIMEOUT_INFERENCE = 120
AZURE_API_VERSION = "2024-10-21"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _az(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run an ``az`` CLI command and return the result."""
    cmd = ["az", *args, "--output", "json"]
    logger.info("[az] %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"az command failed (rc={result.returncode}):\n{result.stderr}")
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


def _get_bearer_token() -> str:
    """Obtain a bearer token for Azure Cognitive Services via ``az``."""
    result = _az(
        "account", "get-access-token",
        "--resource", "https://cognitiveservices.azure.com",
        "--query", "accessToken",
    )
    # Output is JSON-encoded string (with quotes).
    return json.loads(result.stdout)


def _get_signed_in_user_id() -> str:
    """Return the object-id of the signed-in Azure AD user."""
    result = _az("ad", "signed-in-user", "show", "--query", "id")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Resource lifecycle
# ---------------------------------------------------------------------------


def _create_resource_group() -> None:
    logger.info("Creating resource group %s in %s ...", RESOURCE_GROUP, LOCATION)
    _az("group", "create", "--name", RESOURCE_GROUP, "--location", LOCATION)


def _create_foundry_resource() -> None:
    logger.info("Creating AI Services resource %s ...", RESOURCE_NAME)
    _az(
        "cognitiveservices", "account", "create",
        "--name", RESOURCE_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--kind", "AIServices",
        "--sku", "S0",
        "--location", LOCATION,
        "--yes",
        timeout=TIMEOUT_PROVISION,
    )
    # Set custom subdomain (required for REST / SDK access).
    _az(
        "cognitiveservices", "account", "update",
        "--name", RESOURCE_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--custom-domain", RESOURCE_NAME,
        timeout=TIMEOUT_PROVISION,
    )
    logger.info("Foundry resource %s created with custom domain", RESOURCE_NAME)


def _deploy_model() -> None:
    logger.info("Deploying model %s as %s ...", MODEL_NAME, DEPLOYMENT_NAME)
    _az(
        "cognitiveservices", "account", "deployment", "create",
        "--name", RESOURCE_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--deployment-name", DEPLOYMENT_NAME,
        "--model-name", MODEL_NAME,
        "--model-version", MODEL_VERSION,
        "--model-format", "OpenAI",
        "--sku-capacity", str(SKU_CAPACITY),
        "--sku-name", SKU_NAME,
        timeout=TIMEOUT_PROVISION,
    )


def _get_endpoint() -> str:
    """Return the resource endpoint URL."""
    info = _az_json(
        "cognitiveservices", "account", "show",
        "--name", RESOURCE_NAME,
        "--resource-group", RESOURCE_GROUP,
    )
    return info["properties"]["endpoint"]  # type: ignore[index]


def _get_resource_id() -> str:
    """Return the full ARM resource ID."""
    info = _az_json(
        "cognitiveservices", "account", "show",
        "--name", RESOURCE_NAME,
        "--resource-group", RESOURCE_GROUP,
    )
    return info["id"]  # type: ignore[index]


def _assign_openai_user_role() -> None:
    """Assign *Cognitive Services OpenAI User* to the signed-in principal.

    This data-plane role is required when using Entra ID bearer-token auth
    (key-based auth is disabled by subscription policy on most tenants).
    """
    principal_id = _get_signed_in_user_id()
    resource_id = _get_resource_id()
    # Built-in role: Cognitive Services OpenAI User
    role = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
    logger.info("Assigning Cognitive Services OpenAI User to %s ...", principal_id)
    _az(
        "role", "assignment", "create",
        "--assignee-object-id", principal_id,
        "--assignee-principal-type", "User",
        "--role", role,
        "--scope", resource_id,
        check=False,  # May already exist; ignore duplicates.
        timeout=60,
    )


def _delete_deployment() -> None:
    logger.info("Deleting deployment %s ...", DEPLOYMENT_NAME)
    _az(
        "cognitiveservices", "account", "deployment", "delete",
        "--name", RESOURCE_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--deployment-name", DEPLOYMENT_NAME,
        "--yes",
        check=False,
        timeout=TIMEOUT_PROVISION,
    )


def _delete_resource_group() -> None:
    logger.info("Deleting resource group %s ...", RESOURCE_GROUP)
    _az(
        "group", "delete",
        "--name", RESOURCE_GROUP,
        "--yes",
        "--no-wait",
        check=False,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestAzureBYOKViability:
    """End-to-end Azure Foundry + Copilot SDK BYOK viability check.

    Run with ``--run-slow`` to execute (skipped by default).
    All resources are created with a random suffix and deleted in teardown.

    Uses Entra ID bearer-token auth (not API keys) because most Azure
    subscriptions disable key-based auth on Cognitive Services by policy.
    The Copilot SDK's ``ProviderConfig`` supports this via ``bearer_token``.
    """

    _endpoint: str = ""

    # -- fixtures -----------------------------------------------------------

    @pytest.fixture(autouse=True, scope="class")
    def azure_resources(self, request: pytest.FixtureRequest):
        """Provision Azure resources before the test class, delete after."""
        # ---------- setup ----------
        _create_resource_group()
        try:
            _create_foundry_resource()
            _deploy_model()
            _assign_openai_user_role()
            endpoint = _get_endpoint()

            request.cls._endpoint = endpoint

            logger.info("Provisioned: endpoint=%s deployment=%s", endpoint, DEPLOYMENT_NAME)

            # Brief pause for deployment + RBAC propagation.
            logger.info("Waiting 30s for deployment & RBAC propagation ...")
            time.sleep(30)

            yield

        finally:
            # ---------- teardown ----------
            _delete_deployment()
            _delete_resource_group()

    # -- step 3: direct inference ------------------------------------------

    def test_01_direct_inference(self) -> None:
        """Verify the Azure OpenAI deployment works via the openai SDK
        using Entra ID bearer-token auth."""
        from openai import AzureOpenAI

        token = _get_bearer_token()
        client = AzureOpenAI(
            azure_endpoint=self._endpoint,
            azure_ad_token=token,
            api_version=AZURE_API_VERSION,
        )

        resp = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[{"role": "user", "content": "Reply with exactly: BYOK_OK"}],
            max_tokens=20,
            temperature=0,
        )

        text = resp.choices[0].message.content or ""
        logger.info("[direct] response: %s", text)
        assert "BYOK_OK" in text, f"Unexpected response: {text}"

    # -- step 4: copilot SDK BYOK inference --------------------------------

    def test_02_copilot_sdk_byok_inference(self) -> None:
        """Verify the Copilot SDK can call Azure OpenAI via BYOK provider
        using bearer_token auth (Entra ID)."""
        # Create a fresh event loop to avoid interference with pytest-asyncio.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._run_copilot_sdk_byok())
        finally:
            loop.close()

    async def _run_copilot_sdk_byok(self) -> None:
        from copilot import CopilotClient

        # Obtain bearer token for Cognitive Services.
        token = _get_bearer_token()

        endpoint = self._endpoint.rstrip("/")

        # The SDK spawns a CLI binary with stderr=PIPE.  The binary can
        # write enough debug/info output to fill the 64 KB pipe buffer on
        # macOS, which deadlocks the process.  Redirect stderr to a temp
        # file to avoid this.
        stderr_file = tempfile.NamedTemporaryFile(
            mode="w", prefix="copilot_byok_", suffix=".log", delete=False,
        )
        _original_popen = subprocess.Popen

        def _patched_popen(*args, **kwargs):
            if kwargs.get("stderr") == subprocess.PIPE:
                first_arg = args[0] if args else []
                if isinstance(first_arg, list) and any(
                    "copilot" in str(a) for a in first_arg
                ):
                    kwargs["stderr"] = stderr_file
            return _original_popen(*args, **kwargs)

        # Monkey-patch at module level so the copilot SDK picks it up.
        subprocess.Popen = _patched_popen  # type: ignore[misc]

        # Try WITHOUT GitHub auth first (the key question).
        opts: dict = {"log_level": "error", "use_logged_in_user": False}

        github_token = os.getenv("GITHUB_TOKEN", "")
        needs_github_token = False

        client = CopilotClient(opts)
        try:
            await client.start()
            logger.info("[copilot-sdk] CopilotClient started WITHOUT GitHub auth")
        except Exception as exc:
            logger.warning(
                "[copilot-sdk] CopilotClient.start() failed without GitHub auth: %s. "
                "Retrying with GITHUB_TOKEN ...",
                exc,
            )
            needs_github_token = True
            await _safe_stop(client)

            # Retry with GitHub token.
            if not github_token:
                pytest.skip(
                    "Copilot CLI requires GitHub auth even for BYOK, "
                    "but no GITHUB_TOKEN is set."
                )

            opts["github_token"] = github_token
            client = CopilotClient(opts)
            await client.start()

        # Restore original Popen immediately after the CLI process starts.
        subprocess.Popen = _original_popen  # type: ignore[misc]

        try:
            session = await client.create_session({
                "model": DEPLOYMENT_NAME,
                "system_message": {"mode": "replace", "content": "You are a test bot."},
                "hooks": {"on_pre_tool_use": _auto_approve},
                "provider": {
                    "type": "azure",
                    "base_url": endpoint,
                    "bearer_token": token,
                    "azure": {"api_version": AZURE_API_VERSION},
                },
            })

            response = await session.send_and_wait(
                {"prompt": "Reply with exactly: SDK_BYOK_OK"},
                timeout=TIMEOUT_INFERENCE,
            )

            result = None
            if response and hasattr(response, "data"):
                result = getattr(response.data, "content", None)

            logger.info("[copilot-sdk] response: %s", result)
            logger.info(
                "[copilot-sdk] GitHub token required for BYOK: %s", needs_github_token
            )
            assert result is not None, "No response from Copilot SDK BYOK session"
            assert "SDK_BYOK_OK" in result, f"Unexpected response: {result}"

            await session.destroy()
        finally:
            await _safe_stop(client)
            stderr_file.close()
            try:
                os.unlink(stderr_file.name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# SDK helpers
# ---------------------------------------------------------------------------


async def _auto_approve(input_data, invocation):
    return {"permissionDecision": "allow"}


async def _safe_stop(client) -> None:
    try:
        await client.stop()
    except Exception:
        try:
            await client.force_stop()
        except Exception:
            pass
