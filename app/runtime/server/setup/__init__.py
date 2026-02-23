"""Setup wizard -- Azure, deployment, voice, prerequisites, and preflight."""

from __future__ import annotations

from ._routes import SetupRoutes
from .azure import AzureSetupRoutes
from .deploy import DeploymentRoutes
from .preflight import PreflightRoutes
from .prerequisites import PrerequisitesRoutes
from .voice import VoiceSetupRoutes

__all__ = [
    "AzureSetupRoutes",
    "DeploymentRoutes",
    "PreflightRoutes",
    "PrerequisitesRoutes",
    "SetupRoutes",
    "VoiceSetupRoutes",
]
