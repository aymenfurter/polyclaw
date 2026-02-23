"""Security preflight checker -- verifiable runtime identity and secret isolation checks.

Every check runs a real command or environment inspection and reports evidence.
No static claims -- every assertion is verified at runtime.

Identity and RBAC checks live in ``preflight_identity``.
Secret-isolation checks live in ``preflight_secrets``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ...config.settings import cfg
from ..cloud.azure import AzureCLI

logger = logging.getLogger(__name__)

# Elevated RBAC roles that the runtime identity should never hold.
_ELEVATED_ROLES = frozenset({
    "Owner",
    "Contributor",
    "User Access Administrator",
    "Role Based Access Control Administrator",
})

# Type alias for the identity dict passed between preflight check modules.
IdentityInfo = dict[str, Any]


@dataclass
class PreflightCheck:
    """Result of a single security preflight check."""

    id: str
    category: str
    name: str
    status: str = "pending"  # pending | pass | fail | warn | skip
    detail: str = ""
    evidence: str = ""
    command: str = ""


@dataclass
class PreflightResult:
    """Aggregated result of all preflight checks."""

    checks: list[PreflightCheck] = field(default_factory=list)
    run_at: str = ""
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    skipped: int = 0


def add_check(result: PreflightResult, **kwargs: Any) -> PreflightCheck:
    """Create a :class:`PreflightCheck`, append it to *result*, and return it."""
    check = PreflightCheck(**kwargs)
    result.checks.append(check)
    return check


class SecurityPreflightChecker:
    """Run verifiable security checks against the runtime identity and secrets."""

    def __init__(self, az: AzureCLI) -> None:
        self._az = az

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_all(self) -> PreflightResult:
        """Execute all security preflight checks and return evidence."""
        from . import preflight_identity as _id
        from . import preflight_rbac as _rbac
        from . import preflight_secrets as _sec

        result = PreflightResult(run_at=datetime.now(timezone.utc).isoformat())

        # Gate: is Azure CLI logged in?
        if not _id.check_azure_logged_in(self._az, result):
            _id.skip_azure_checks(result)
            _sec.run_secret_checks(result)
            self._tally(result)
            return result

        # Identity verification
        identity = _id.check_identity_configured(self._az, result)
        if identity:
            _id.check_identity_valid(self._az, result, identity)
            _id.check_credential_expiry(self._az, result, identity)

            # RBAC verification
            assignments = _rbac.check_rbac_list(self._az, result, identity)
            if assignments is not None:
                bot_rg = cfg.env.read("BOT_RESOURCE_GROUP") or ""
                _rbac.check_rbac_has_role(
                    result, assignments, "Azure Bot Service Contributor Role",
                    "rbac_bot_contributor", "Azure Bot Service Contributor Role", bot_rg,
                )
                _rbac.check_rbac_has_role(
                    result, assignments, "Reader",
                    "rbac_reader", "Reader Role", bot_rg,
                )
                _rbac.check_rbac_kv_access(result, assignments, identity)
                if identity.get("strategy") == "managed_identity":
                    _rbac.check_rbac_has_role(
                        result, assignments, "Cognitive Services OpenAI User",
                        "rbac_aoai_user", "Azure OpenAI Access",
                        "",
                        missing_severity="warn",
                        missing_detail="Needed for identity-auth voice",
                    )
                _rbac.check_rbac_session_pool(result, assignments)
                _rbac.check_rbac_no_elevated(result, assignments)
                _rbac.check_rbac_scope_contained(result, assignments)

        # Secret isolation
        _sec.run_secret_checks(result)

        self._tally(result)
        return result

    @staticmethod
    def to_dict(result: PreflightResult) -> dict[str, Any]:
        """Serialize a *PreflightResult* to a JSON-safe dict."""
        return {
            "checks": [asdict(c) for c in result.checks],
            "run_at": result.run_at,
            "passed": result.passed,
            "failed": result.failed,
            "warnings": result.warnings,
            "skipped": result.skipped,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tally(result: PreflightResult) -> None:
        for c in result.checks:
            if c.status == "pass":
                result.passed += 1
            elif c.status == "fail":
                result.failed += 1
            elif c.status == "warn":
                result.warnings += 1
            elif c.status == "skip":
                result.skipped += 1
