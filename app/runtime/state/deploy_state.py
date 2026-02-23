"""Deployment state tracking -- unified registry of all environments.

Tracks every deployment (local Docker, ACA, or future targets) with a
unique deploy_id and matching Azure resource tag prefix.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..config.settings import cfg

logger = logging.getLogger(__name__)

TAG_PREFIX = "polycl"


def generate_deploy_id() -> str:
    return secrets.token_hex(4)


def deploy_tag(deploy_id: str) -> str:
    return f"{TAG_PREFIX}-{deploy_id}"


@dataclass
class ResourceEntry:
    resource_type: str = ""
    resource_group: str = ""
    resource_name: str = ""
    resource_id: str = ""
    purpose: str = ""
    created_at: str = ""


@dataclass
class DeploymentRecord:
    deploy_id: str = ""
    tag: str = ""
    kind: Literal["local", "aca"] = "local"
    created_at: str = ""
    updated_at: str = ""
    status: Literal["active", "stopped", "destroyed"] = "active"
    resource_groups: list[str] = field(default_factory=list)
    resources: list[ResourceEntry] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new(
        kind: Literal["local", "aca"],
        deploy_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> DeploymentRecord:
        did = deploy_id or generate_deploy_id()
        now = datetime.now(UTC).isoformat()
        return DeploymentRecord(
            deploy_id=did,
            tag=deploy_tag(did),
            kind=kind,
            created_at=now,
            updated_at=now,
            status="active",
            config=config or {},
        )

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()

    def add_resource(
        self,
        resource_type: str,
        resource_group: str,
        resource_name: str,
        purpose: str = "",
        resource_id: str = "",
    ) -> ResourceEntry:
        entry = ResourceEntry(
            resource_type=resource_type,
            resource_group=resource_group,
            resource_name=resource_name,
            resource_id=resource_id,
            purpose=purpose,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.resources.append(entry)
        if resource_group and resource_group not in self.resource_groups:
            self.resource_groups.append(resource_group)
        self.touch()
        return entry

    def mark_destroyed(self) -> None:
        self.status = "destroyed"
        self.touch()

    def mark_stopped(self) -> None:
        self.status = "stopped"
        self.touch()


class DeployStateStore:
    """JSON-file-backed deployment state registry."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cfg.data_dir / "deployments.json")
        self._deployments: dict[str, DeploymentRecord] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def all_deployments(self) -> dict[str, DeploymentRecord]:
        return dict(self._deployments)

    def get(self, deploy_id: str) -> DeploymentRecord | None:
        return self._deployments.get(deploy_id)

    def active_deployments(self) -> list[DeploymentRecord]:
        return [d for d in self._deployments.values() if d.status == "active"]

    def by_kind(self, kind: str) -> list[DeploymentRecord]:
        return [d for d in self._deployments.values() if d.kind == kind]

    def current_local(self) -> DeploymentRecord | None:
        local = [
            d for d in self._deployments.values()
            if d.kind == "local" and d.status == "active"
        ]
        return max(local, key=lambda d: d.updated_at) if local else None

    def current_aca(self) -> DeploymentRecord | None:
        aca = [
            d for d in self._deployments.values()
            if d.kind == "aca" and d.status == "active"
        ]
        return max(aca, key=lambda d: d.updated_at) if aca else None

    def register(self, record: DeploymentRecord) -> None:
        self._deployments[record.deploy_id] = record
        self._save()
        logger.info(
            "Registered deployment %s (kind=%s, tag=%s)",
            record.deploy_id, record.kind, record.tag,
        )

    def update(self, record: DeploymentRecord) -> None:
        record.touch()
        self._deployments[record.deploy_id] = record
        self._save()

    def mark_destroyed(self, deploy_id: str) -> None:
        rec = self._deployments.get(deploy_id)
        if rec:
            rec.mark_destroyed()
            self._save()
            logger.info("Deployment %s marked as destroyed", deploy_id)

    def remove(self, deploy_id: str) -> bool:
        if deploy_id in self._deployments:
            del self._deployments[deploy_id]
            self._save()
            logger.info("Deployment %s removed from state", deploy_id)
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"deployments": {did: asdict(rec) for did, rec in self._deployments.items()}}

    def summary(self) -> list[dict[str, Any]]:
        result = []
        for rec in self._deployments.values():
            result.append({
                "deploy_id": rec.deploy_id,
                "tag": rec.tag,
                "kind": rec.kind,
                "status": rec.status,
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
                "resource_groups": rec.resource_groups,
                "resource_count": len(rec.resources),
            })
        return sorted(result, key=lambda r: r["updated_at"], reverse=True)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            for did, rec_data in raw.get("deployments", {}).items():
                resources = [ResourceEntry(**r) for r in rec_data.pop("resources", [])]
                rec = DeploymentRecord(**{
                    k: v for k, v in rec_data.items()
                    if k in DeploymentRecord.__dataclass_fields__
                })
                rec.resources = resources
                self._deployments[did] = rec
        except Exception as exc:
            logger.warning(
                "Failed to load deploy state from %s: %s", self._path, exc, exc_info=True,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
