"""CLI for environment / deployment management."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .services.cloud.azure import AzureCLI
from .services.security.misconfig_checker import MisconfigChecker
from .services.resource_tracker import ResourceTracker
from .state.deploy_state import DeployStateStore


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def _cyan(text: str) -> str:
    return f"\033[36m{text}\033[0m"


def _severity_color(severity: str) -> str:
    colors = {"critical": _red, "high": _red, "medium": _yellow, "low": _cyan, "info": _green}
    return colors.get(severity, str)(severity.upper())


def _status_color(status: str) -> str:
    if status == "active":
        return _green(status)
    if status == "destroyed":
        return _red(status)
    return _yellow(status)


def cmd_list(_args: argparse.Namespace) -> None:
    store = DeployStateStore()
    deployments = store.summary()
    if not deployments:
        print("No deployments registered.")
        return

    fmt = "{:<12} {:<8} {:<10} {:<20} {:<24} {}"
    print(_bold(fmt.format("DEPLOY_ID", "KIND", "STATUS", "TAG", "UPDATED", "RESOURCE GROUPS")))
    print("-" * 100)

    for d in deployments:
        rgs = ", ".join(d["resource_groups"]) if d["resource_groups"] else "-"
        print(fmt.format(
            d["deploy_id"], d["kind"], _status_color(d["status"]),
            d["tag"], d["updated_at"][:19], rgs,
        ))


def cmd_show(args: argparse.Namespace) -> None:
    store = DeployStateStore()
    rec = store.get(args.deploy_id)
    if not rec:
        print(f"Deployment '{args.deploy_id}' not found.")
        sys.exit(1)

    if args.json:
        print(json.dumps(asdict(rec), indent=2))
        return

    print(f"{_bold('Deploy ID:')}  {rec.deploy_id}")
    print(f"{_bold('Tag:')}        {rec.tag}")
    print(f"{_bold('Kind:')}       {rec.kind}")
    print(f"{_bold('Status:')}     {_status_color(rec.status)}")
    print(f"{_bold('Created:')}    {rec.created_at}")
    print(f"{_bold('Updated:')}    {rec.updated_at}")
    print(f"{_bold('RGs:')}        {', '.join(rec.resource_groups) or '-'}")
    print()

    if rec.resources:
        print(_bold("Resources:"))
        for r in rec.resources:
            print(f"  - [{r.resource_type}] {r.resource_name} in {r.resource_group}")
            if r.purpose:
                print(f"    Purpose: {r.purpose}")
    else:
        print("No resources tracked.")

    if rec.config:
        print()
        print(_bold("Config:"))
        print(json.dumps(rec.config, indent=2))


def cmd_audit(args: argparse.Namespace) -> None:
    az = AzureCLI()
    store = DeployStateStore()
    tracker = ResourceTracker(az, store)

    print("Scanning Azure subscription for resources...")
    result = tracker.audit()

    if args.json:
        print(json.dumps(tracker.to_dict(result), indent=2))
        return

    print()
    print(_bold(f"Known deployments: {len(result.known_deploy_ids)}"))
    for did in result.known_deploy_ids:
        print(f"  - {did}")

    print()
    print(_bold(f"Tracked resources: {len(result.tracked_resources)}"))
    for r in result.tracked_resources:
        print(f"  [{r.resource_type}] {r.name} ({r.resource_group})")

    print()
    if result.orphaned_groups:
        print(_red(_bold(f"Orphaned resource groups: {len(result.orphaned_groups)}")))
        for g in result.orphaned_groups:
            tag_info = f" (tag: {g.deploy_tag})" if g.deploy_tag else ""
            print(f"  - {g.name} [{g.location}]{tag_info}")
    else:
        print(_green("No orphaned resource groups found."))

    if result.orphaned_resources:
        print()
        print(_red(_bold(f"Orphaned resources: {len(result.orphaned_resources)}")))
        for r in result.orphaned_resources:
            print(f"  [{r.resource_type}] {r.name} ({r.resource_group})")
    elif not result.orphaned_groups:
        print(_green("No orphaned resources found."))

    if result.unknown_deploy_ids:
        print()
        print(_yellow(f"Unknown deploy IDs (not in local state): {result.unknown_deploy_ids}"))


def cmd_misconfig(args: argparse.Namespace) -> None:
    az = AzureCLI()
    store = DeployStateStore()
    checker = MisconfigChecker(az)

    resource_groups: list[str] = []
    if args.deploy_id:
        rec = store.get(args.deploy_id)
        if not rec:
            print(f"Deployment '{args.deploy_id}' not found.")
            sys.exit(1)
        resource_groups = rec.resource_groups
        print(f"Scanning resource groups for deployment {args.deploy_id}...")
    else:
        for rec in store.all_deployments.values():
            resource_groups.extend(rec.resource_groups)
        resource_groups = list(set(resource_groups))
        print(f"Scanning all tracked resource groups ({len(resource_groups)})...")

    if not resource_groups:
        print("No resource groups to scan.")
        return

    result = checker.check_all(resource_groups)

    if args.json:
        print(json.dumps(MisconfigChecker.to_dict(result), indent=2))
        return

    print()
    print(f"{_bold('Resources scanned:')} {result.resources_scanned}")
    print(f"{_bold('Checks passed:')}     {_green(str(result.checks_passed))}")
    print(f"{_bold('Checks failed:')}     {_red(str(result.checks_failed)) if result.checks_failed else _green('0')}")

    if result.findings:
        print()
        print(_bold("Findings:"))
        for f in result.findings:
            print(f"  {_severity_color(f.severity)} [{f.category}] {f.resource_name} ({f.resource_group})")
            print(f"    {_bold(f.title)}")
            print(f"    {f.detail}")
            print(f"    Fix: {_cyan(f.recommendation)}")
            print()
    else:
        print()
        print(_green("No misconfigurations found."))


def cmd_cleanup(args: argparse.Namespace) -> None:
    az = AzureCLI()
    store = DeployStateStore()
    tracker = ResourceTracker(az, store)

    rec = store.get(args.deploy_id)
    if not rec:
        print(f"Deployment '{args.deploy_id}' not found.")
        sys.exit(1)

    if not args.yes:
        print(f"This will delete all Azure resource groups for deployment {args.deploy_id}:")
        for rg in rec.resource_groups:
            print(f"  - {rg}")
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    steps = tracker.cleanup_deployment(args.deploy_id)
    for s in steps:
        status = _green(s["status"]) if s["status"] == "ok" else _red(s["status"])
        print(f"  {s['step']}: {status} - {s.get('detail', '')}")


def cmd_remove(args: argparse.Namespace) -> None:
    store = DeployStateStore()
    if store.remove(args.deploy_id):
        print(f"Record for deployment '{args.deploy_id}' removed.")
    else:
        print(f"Deployment '{args.deploy_id}' not found.")
        sys.exit(1)


def cmd_cleanup_orphans(args: argparse.Namespace) -> None:
    az = AzureCLI()
    store = DeployStateStore()
    tracker = ResourceTracker(az, store)

    print("Running audit to find orphaned resource groups...")
    result = tracker.audit()

    if not result.orphaned_groups:
        print(_green("No orphaned resource groups found."))
        return

    print(f"Found {len(result.orphaned_groups)} orphaned resource group(s):")
    for g in result.orphaned_groups:
        print(f"  - {g.name}")

    if not args.yes:
        answer = input("Delete all orphaned resource groups? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    for g in result.orphaned_groups:
        ok, msg = tracker.cleanup_orphan_group(g.name)
        status = _green("ok") if ok else _red("failed")
        detail = f"Deleting {g.name}" if ok else msg
        print(f"  {g.name}: {status} - {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="polyclaw-env",
        description="Manage deployment environments and Azure resources.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all deployments")

    p_show = sub.add_parser("show", help="Show deployment details")
    p_show.add_argument("deploy_id", help="Deployment ID")
    p_show.add_argument("--json", action="store_true", help="Output as JSON")

    p_audit = sub.add_parser("audit", help="Audit Azure resources for orphans")
    p_audit.add_argument("--json", action="store_true", help="Output as JSON")

    p_misconfig = sub.add_parser("misconfig", help="Run misconfiguration checks")
    p_misconfig.add_argument("deploy_id", nargs="?", help="Deployment ID (optional)")
    p_misconfig.add_argument("--json", action="store_true", help="Output as JSON")

    p_cleanup = sub.add_parser("cleanup", help="Destroy a deployment's Azure resources")
    p_cleanup.add_argument("deploy_id", help="Deployment ID")
    p_cleanup.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    p_remove = sub.add_parser("remove", help="Remove deployment record (no Azure ops)")
    p_remove.add_argument("deploy_id", help="Deployment ID")

    p_orphans = sub.add_parser("cleanup-orphans", help="Delete all orphaned resource groups")
    p_orphans.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args()
    commands = {
        "list": cmd_list, "show": cmd_show, "audit": cmd_audit,
        "misconfig": cmd_misconfig, "cleanup": cmd_cleanup,
        "remove": cmd_remove, "cleanup-orphans": cmd_cleanup_orphans,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
