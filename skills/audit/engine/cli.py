import argparse
import json
import os

from .version import version
from .profiles import profile_names
from .c_io import check_platform_support


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engine")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="command")
    audit = sub.add_parser("audit")
    audit.add_argument("profile_name", nargs="?", choices=profile_names(), metavar="profile")
    audit.add_argument("--full", action="store_true")
    audit.add_argument("--accept-baseline", action="store_true")
    audit.add_argument("--profile", choices=profile_names())
    audit.add_argument("--ask-backend", action="store_true")
    audit.add_argument("--backend")
    audit.add_argument("--fast", action="store_true")
    audit.add_argument("--repo-root", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    scope = sub.add_parser("scope")
    scope.add_argument("--full", action="store_true")
    scope.add_argument("--profile", choices=profile_names())
    scope.add_argument("--repo-root", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--repo-root", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    resume.add_argument("--abandon", action="store_true")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--repo-root", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    migrate.add_argument("--dry-run", action="store_true")
    update = sub.add_parser("update")
    update.add_argument("--source")
    update.add_argument("--install-dir")
    commands = update.add_subparsers(dest="update_command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--hook", action="store_true")
    notify = commands.add_parser("notify")
    notify.add_argument("setting", choices=("on", "off"))
    plan = commands.add_parser("plan")
    plan.add_argument("--to")
    apply = commands.add_parser("apply")
    apply.add_argument("--to", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--dry-run", action="store_true")
    for action in (check, notify, plan, apply, rollback):
        action.add_argument("--source", default=argparse.SUPPRESS)
        action.add_argument("--install-dir", default=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    try:
        check_platform_support()
    except Exception as exc:
        import sys
        arguments = sys.argv[1:] if argv is None else argv
        if "--hook" in arguments:
            args = parser().parse_args(arguments)
            if args.command == "update" and args.update_command == "check" and args.hook:
                return 0
        reason = getattr(exc, "reason", "io-unsupported-platform")
        print(json.dumps({"nextAction": "abort", "runId": None, "outcome": None, "reason": reason}, sort_keys=True, separators=(",", ":")))
        return 3
    p = parser()
    args = p.parse_args(argv)
    if args.command == "audit" and args.profile_name is not None:
        if args.profile is not None and args.profile != args.profile_name:
            p.error("positional profile and --profile must match")
        args.profile = args.profile_name
    if args.version:
        print(version())
        return 0
    if args.command is None:
        parser().error("a command is required")
    if args.command == "update" and args.update_command == "check" and args.hook:
        try:
            from . import c_update
            message = c_update.hook_message(args)
            if message is not None:
                print(json.dumps(message, ensure_ascii=False))
        except Exception:
            pass
        return 0
    if args.command == "scope":
        from . import c_scope
        try:
            output = c_scope.preview(args.repo_root, full=args.full, profile=args.profile)
            code = 0
        except Exception as exc:
            output = {"nextAction": "abort", "runId": None, "outcome": None,
                      "reason": getattr(exc, "reason", type(exc).__name__)}
            code = 3
        print(json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        return code
    from . import c_engine, c_migrate, c_run
    try:
        if args.command == "migrate":
            result = c_migrate.migrate(args.repo_root, dry_run=args.dry_run)
        elif args.command == "update":
            from . import c_update
            result = c_update.run(args)
        elif args.command == "audit":
            result = c_engine.run(args.repo_root, full=args.full, profile=args.profile, accept_baseline=args.accept_baseline,
                                  backend=args.backend, fast=args.fast, ask_backend=args.ask_backend)
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
        "requestSeq", "requestPath", "choices",
    )
    if args.command == "migrate":
        output_fields += ("config", "counts", "inputs", "anchor", "runOpen")
    if args.command == "update":
        output_fields += ("installed", "latest", "newer", "checkedAt", "source", "target", "targetCommit", "notes", "log", "diffstat", "from", "to", "installDir", "asideDir", "stagingDir", "checks", "notify")
    output = {key: result[key] for key in output_fields if key in result}
    print(json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return code
