"""Dry-run deployment/rollback plan for the limited scalar candidate.

This command intentionally does not stop or start services.  The current host
has no configured service manager, so an executable process mutation would be
unsafe until the user approves a maintenance window and the operator supplies
the exact process identity.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from app.config import Settings
from app.semantic_v2.limited_scalar_runtime import (
    OAGNET_RUNTIME_FILES,
    SQL_RUNTIME_FILES,
    source_bundle_digest,
    validate_limited_scalar_settings,
)


ROUND_511_BASELINE = "6b3a6e91e06ee5897a44ca2fd5d28532ecb68a79"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--action", choices=("source-digests", "check", "enable-plan", "rollback-plan"),
                       default="check")
    value.add_argument("--source-root", type=Path,
                       default=Path(__file__).resolve().parents[1])
    value.add_argument("--v1-source-root", type=Path)
    value.add_argument("--expected-current-pid", type=int)
    value.add_argument("--execute", action="store_true")
    value.add_argument("--approval-id")
    return value


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        timeout=10, check=True,
    )
    return completed.stdout.strip()


def run(args) -> dict:
    if args.execute:
        raise SystemExit(
            "execution is disabled in Round 5.12; use this verified plan only after "
            "explicit user approval and service-owner maintenance orchestration"
        )
    root = args.source_root.resolve()
    settings = Settings()
    if args.action == "source-digests":
        return {
            "dry_run": True,
            "action": args.action,
            "oagnet_source_digest": source_bundle_digest(
                settings.limited_scalar_oagnet_root, OAGNET_RUNTIME_FILES
            ),
            "sql_source_digest": source_bundle_digest(
                settings.limited_scalar_sql_translator_root, SQL_RUNTIME_FILES
            ),
            "model_called": False,
            "business_sql_executed": False,
            "service_process_changed": False,
        }
    head = _git(root, "rev-parse", "HEAD") if (root / ".git").exists() else "WORKSPACE_NOT_GIT"
    ancestry = None
    if head != "WORKSPACE_NOT_GIT":
        ancestry = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor",
             ROUND_511_BASELINE, head], timeout=10,
        ).returncode == 0
    config = None
    config_error = None
    try:
        config = validate_limited_scalar_settings(settings)
    except Exception as exc:
        config_error = str(exc)
    common = {
        "dry_run": True,
        "action": args.action,
        "source_root": str(root),
        "git_head": head,
        "contains_round_5_11": ancestry,
        "runtime_mode": settings.runtime_mode,
        "configuration_valid": config_error is None,
        "configuration_error": config_error,
        "configuration_receipt": config,
        "business_sql_executed": False,
        "model_called": False,
        "service_process_changed": False,
    }
    if args.action == "enable-plan":
        common["preconditions"] = [
            "explicit user approval and maintenance window",
            "deploy a commit containing Round 5.11 and Round 5.12",
            "set DATA_AGENT_RUNTIME_MODE=V2_LIMITED_SCALAR and all pinned candidate settings",
            "use a new conversation; do not migrate V1 session state",
            "verify the exact current Agent PID/command before stopping it",
        ]
        common["candidate_command"] = [
            "python", "-X", "utf8", "-u", "-m", "uvicorn",
            "app.main:app", "--host", "0.0.0.0", "--port", "8088",
        ]
        common["post_start_checks"] = ["GET /live", "GET /ready"]
        common["expected_current_pid"] = args.expected_current_pid
    elif args.action == "rollback-plan":
        if args.v1_source_root is None:
            common["rollback_ready"] = False
            common["rollback_blocker"] = "--v1-source-root is required"
        else:
            common["rollback_ready"] = True
            common["v1_source_root"] = str(args.v1_source_root.resolve())
            common["steps"] = [
                "stop only the recorded candidate PID",
                "start the recorded V1 source with DATA_AGENT_RUNTIME_MODE=V1",
                "preserve current secret configuration",
                "do not delete or translate the V2 namespace",
                "require a new V1 conversation after rollback",
                "verify GET /live and GET /ready",
            ]
    return common


def main() -> None:
    print(json.dumps(run(parser().parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
