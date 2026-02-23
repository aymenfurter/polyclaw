"""Azure resource tracker -- discover and audit cloud resources by tag."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..state.deploy_state import TAG_PREFIX, DeployStateStore
from .cloud.azure import AzureCLI

logger = logging.getLogger(__name__)

POLYCLAW_RG_PREFIXES = ("polyclaw-",)


@dataclass
class AzureResource:
    id: str = ""
    name: str = ""
    resource_group: str = ""
    resource_type: str = ""
    location: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    deploy_tag: str = ""


@dataclass
class ResourceGroup:
    name: str = ""
    location: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    deploy_tag: str = ""


@dataclass
class AuditResult:
    tracked_resources: list[AzureResource] = field(default_factory=list)
    orphaned_resources: list[AzureResource] = field(default_factory=list)
    orphaned_groups: list[ResourceGroup] = field(default_factory=list)
    known_deploy_ids: list[str] = field(default_factory=list)
    unknown_deploy_ids: list[str] = field(default_factory=list)


class ResourceTracker:
    """Discover, audit, and clean up Azure resources."""

    def __init__(self, az: AzureCLI, store: DeployStateStore) -> None:
        self._az = az
        self._store = store

    def discover_tagged_resource_groups(self) -> list[ResourceGroup]:
        groups = self._az.json("group", "list") or []
        result: list[ResourceGroup] = []
        if not isinstance(groups, list):
            return result
        for g in groups:
            tags = g.get("tags") or {}
            dtag = tags.get("polyclaw_deploy", "")
            name = g.get("name", "")
            if dtag or any(name.startswith(p) for p in POLYCLAW_RG_PREFIXES):
                result.append(ResourceGroup(
                    name=name, location=g.get("location", ""),
                    tags=tags, deploy_tag=dtag,
                ))
        return result

    def discover_resources_in_group(self, rg: str) -> list[AzureResource]:
        resources = self._az.json("resource", "list", "--resource-group", rg) or []
        result: list[AzureResource] = []
        if not isinstance(resources, list):
            return result
        for r in resources:
            tags = r.get("tags") or {}
            result.append(AzureResource(
                id=r.get("id", ""), name=r.get("name", ""), resource_group=rg,
                resource_type=r.get("type", ""), location=r.get("location", ""),
                tags=tags, deploy_tag=tags.get("polyclaw_deploy", ""),
            ))
        return result

    def discover_all_polyclaw_resources(self) -> list[AzureResource]:
        resources = self._az.json("resource", "list", "--tag", "polyclaw_deploy") or []
        result: list[AzureResource] = []
        if not isinstance(resources, list):
            return result
        for r in resources:
            tags = r.get("tags") or {}
            rid = r.get("id", "")
            rg = ""
            parts = rid.split("/")
            for i, p in enumerate(parts):
                if p.lower() == "resourcegroups" and i + 1 < len(parts):
                    rg = parts[i + 1]
                    break
            result.append(AzureResource(
                id=rid, name=r.get("name", ""), resource_group=rg,
                resource_type=r.get("type", ""), location=r.get("location", ""),
                tags=tags, deploy_tag=tags.get("polyclaw_deploy", ""),
            ))
        return result

    def audit(self) -> AuditResult:
        result = AuditResult()
        known_ids = set(self._store.all_deployments.keys())
        result.known_deploy_ids = list(known_ids)
        groups = self.discover_tagged_resource_groups()
        seen_deploy_ids: set[str] = set()

        for g in groups:
            dtag = g.deploy_tag
            if dtag:
                did = dtag.replace(f"{TAG_PREFIX}-", "") if dtag.startswith(TAG_PREFIX) else ""
                if did:
                    seen_deploy_ids.add(did)
                    if did not in known_ids:
                        result.orphaned_groups.append(g)
                        result.unknown_deploy_ids.append(did)
                    else:
                        resources = self.discover_resources_in_group(g.name)
                        result.tracked_resources.extend(resources)
            else:
                resources = self.discover_resources_in_group(g.name)
                if resources:
                    result.orphaned_resources.extend(resources)
                result.orphaned_groups.append(g)

        all_tagged = self.discover_all_polyclaw_resources()
        tracked_ids = {r.id for r in result.tracked_resources}
        for r in all_tagged:
            if r.id not in tracked_ids:
                dtag = r.deploy_tag
                did = dtag.replace(f"{TAG_PREFIX}-", "") if dtag.startswith(TAG_PREFIX) else ""
                if did and did not in known_ids:
                    result.orphaned_resources.append(r)
                    if did not in result.unknown_deploy_ids:
                        result.unknown_deploy_ids.append(did)

        result.unknown_deploy_ids = list(set(result.unknown_deploy_ids))
        return result

    def reconcile(self) -> list[dict[str, str]]:
        active = self._store.active_deployments()
        if not active:
            return []

        all_groups = self._az.json("group", "list", "--query", "[].name") or []
        existing_rgs: set[str] = set()
        if isinstance(all_groups, list):
            existing_rgs = {str(g) for g in all_groups}

        live_resources_by_rg: dict[str, set[str]] = {}

        def _live_names(rg: str) -> set[str]:
            if rg not in live_resources_by_rg:
                items = self._az.json("resource", "list", "--resource-group", rg, "--query", "[].name") or []
                live_resources_by_rg[rg] = {str(n) for n in items} if isinstance(items, list) else set()
            return live_resources_by_rg[rg]

        changes: list[dict[str, str]] = []

        for rec in active:
            if not rec.resource_groups and not rec.resources:
                continue

            alive_rgs = [rg for rg in rec.resource_groups if rg in existing_rgs]
            dead_rgs = set(rec.resource_groups) - set(alive_rgs)

            if rec.resource_groups and not alive_rgs:
                self._store.remove(rec.deploy_id)
                changes.append({
                    "deploy_id": rec.deploy_id, "tag": rec.tag,
                    "action": "removed", "detail": "all resource groups deleted",
                })
                logger.info("Reconcile: deployment %s (%s) has no surviving RGs -- removed", rec.deploy_id, rec.tag)
                continue

            dirty = False

            if dead_rgs:
                rec.resource_groups = alive_rgs
                dirty = True
                changes.append({
                    "deploy_id": rec.deploy_id, "tag": rec.tag,
                    "action": "pruned_rgs", "detail": ", ".join(sorted(dead_rgs)),
                })
                logger.info("Reconcile: deployment %s -- removed dead RGs: %s", rec.deploy_id, ", ".join(sorted(dead_rgs)))

            surviving: list = []
            pruned_names: list[str] = []
            for entry in rec.resources:
                rg = entry.resource_group
                if rg and rg in dead_rgs:
                    pruned_names.append(entry.resource_name)
                    continue
                if rg and rg in existing_rgs:
                    if entry.resource_name and entry.resource_name not in _live_names(rg):
                        pruned_names.append(entry.resource_name)
                        continue
                surviving.append(entry)

            if pruned_names:
                rec.resources = surviving
                dirty = True
                changes.append({
                    "deploy_id": rec.deploy_id, "tag": rec.tag,
                    "action": "pruned_resources", "detail": ", ".join(pruned_names),
                })
                logger.info("Reconcile: deployment %s -- removed stale resources: %s", rec.deploy_id, ", ".join(pruned_names))

            if dirty:
                self._store.update(rec)

        if changes:
            logger.info("Reconcile: %d change(s) applied", len(changes))
        else:
            logger.info("Reconcile: all %d active deployment(s) verified", len(active))

        return changes

    def delete_resource_group(self, rg: str) -> tuple[bool, str]:
        ok, msg = self._az.ok("group", "delete", "--name", rg, "--yes", "--no-wait")
        if ok:
            logger.info("Initiated deletion of resource group '%s'", rg)
        else:
            logger.error("Failed to delete resource group '%s': %s", rg, msg)
        return ok, msg

    def cleanup_deployment(self, deploy_id: str) -> list[dict[str, Any]]:
        rec = self._store.get(deploy_id)
        steps: list[dict[str, Any]] = []
        if not rec:
            steps.append({"step": "lookup", "status": "failed", "detail": f"Deployment {deploy_id} not found"})
            return steps
        for rg in rec.resource_groups:
            ok, msg = self.delete_resource_group(rg)
            steps.append({
                "step": f"delete_rg_{rg}",
                "status": "ok" if ok else "failed",
                "detail": f"Deleting {rg}" if ok else msg,
            })
        self._store.mark_destroyed(deploy_id)
        steps.append({"step": "mark_destroyed", "status": "ok", "detail": deploy_id})
        return steps

    def cleanup_orphan_group(self, rg: str) -> tuple[bool, str]:
        return self.delete_resource_group(rg)

    def to_dict(self, audit_result: AuditResult) -> dict[str, Any]:
        return {
            "tracked_resources": [
                {"id": r.id, "name": r.name, "resource_group": r.resource_group,
                 "resource_type": r.resource_type, "location": r.location, "deploy_tag": r.deploy_tag}
                for r in audit_result.tracked_resources
            ],
            "orphaned_resources": [
                {"id": r.id, "name": r.name, "resource_group": r.resource_group,
                 "resource_type": r.resource_type, "location": r.location, "deploy_tag": r.deploy_tag}
                for r in audit_result.orphaned_resources
            ],
            "orphaned_groups": [
                {"name": g.name, "location": g.location, "deploy_tag": g.deploy_tag}
                for g in audit_result.orphaned_groups
            ],
            "known_deploy_ids": audit_result.known_deploy_ids,
            "unknown_deploy_ids": audit_result.unknown_deploy_ids,
        }
