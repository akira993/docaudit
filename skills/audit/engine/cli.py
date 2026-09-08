import argparse
import json
import os

from .version import version
from .profiles import PROFILE_TABLE
from .c_io import check_platform_support


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engine")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="command")
    audit = sub.add_parser("audit")
    audit.add_argument("--full", action="store_true")
    audit.add_argument("--profile", choices=tuple(row["name"] for row in PROFILE_TABLE))
    audit.add_argument("--repo-root", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--repo-root", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    resume.add_argument("--abandon", action="store_true")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--repo-root", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    migrate.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None) -> int:
    try:
        check_platform_support()
    except Exception as exc:
        reason = getattr(exc, "reason", "io-unsupported-platform")
        print(json.dumps({"nextAction": "abort", "runId": None, "outcome": None, "reason": reason}, sort_keys=True, separators=(",", ":")))
        return 3
    args = parser().parse_args(argv)
    if args.version:
        print(version())
        return 0
    if args.command is None:
        parser().error("a command is required")
    from . import c_engine, c_migrate, c_run
    try:
        if args.command == "migrate":
            result = c_migrate.migrate(args.repo_root, dry_run=args.dry_run)
        elif args.command == "audit":
            result = c_engine.run(args.repo_root, full=args.full, profile=args.profile)
        elif args.abandon:
            c_run.abandon(args.repo_root, args.run_id)
            result = {"exitCode": 0, "nextAction": "done", "runId": args.run_id,
                      "outcome": "undecided", "reason": "abandoned"}
        else:
            result = c_engine.resume(args.repo_root, args.run_id)
    except c_run.RunRejected as exc:
        result = {"exitCode": 3, "nextAction": "abort", "runId": None,
                  "outcome": None, "reason": exc.reason}
    except Exception as exc:
        result = {"exitCode": 4, "nextAction": "abort", "runId": getattr(exc, "run_id", None),
                  "outcome": None, "reason": getattr(exc, "reason", type(exc).__name__)}
    code = int(result.get("exitCode", 4))
    output_fields = (
        "nextAction", "runId", "outcome", "reason", "reportPath",
        "requestSeq", "requestPath",
    )
    if args.command == "migrate":
        output_fields += ("config", "counts", "inputs", "anchor", "runOpen")
    output = {key: result[key] for key in output_fields if key in result}
    print(json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return code
