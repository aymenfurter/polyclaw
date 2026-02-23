"""External service integrations.

Re-exports are lazy to avoid a circular import: the ``Settings`` singleton
imports ``services.keyvault`` during init, and the cloud / deployment /
security sub-packages import ``cfg`` at module level.  Deferring those
imports via ``__getattr__`` means only ``keyvault`` is loaded while
``Settings()`` runs; the heavier sub-packages load on first access, by
which time ``cfg`` is assigned.
"""

from __future__ import annotations

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AzureCLI":                 (".cloud",      "AzureCLI"),
    "GitHubAuth":               (".cloud",      "GitHubAuth"),
    "AcaDeployer":              (".deployment", "AcaDeployer"),
    "BotDeployer":              (".deployment", "BotDeployer"),
    "Provisioner":              (".deployment", "Provisioner"),
    "MisconfigChecker":         (".security",   "MisconfigChecker"),
    "PromptShieldService":      (".security",   "PromptShieldService"),
    "SecurityPreflightChecker": (".security",   "SecurityPreflightChecker"),
    "CloudflareTunnel":         (".tunnel",     "CloudflareTunnel"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        import importlib

        subpkg, attr = _LAZY_IMPORTS[name]
        mod = importlib.import_module(subpkg, __name__)
        val = getattr(mod, attr)
        globals()[name] = val  # cache for subsequent access
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_LAZY_IMPORTS)
