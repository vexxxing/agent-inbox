"""Command line: python -m agent_inbox <command>

  watch      check due watchers and post hits to the inbox
  relay      turn the current Actions trigger into an inbox event
  setup      create the inbox pull request (idempotent)
  send       fire an event at the relay from anywhere (repository_dispatch)
  post       post an event straight to the inbox (needs a token that can comment)
  parse      read an inbox comment on stdin, print the event JSON (or nothing)
  check      validate watchers/*.yml and inbox.yml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import event as ev


def _post_fn(args):
    if args.dry_run:
        def post(event):
            print(ev.to_comment(event))
        return post
    from .github import GitHub
    github = GitHub.from_env()
    number = github.find_inbox()
    return lambda event: github.post_event(event, number=number)


def cmd_watch(args) -> int:
    from . import watch
    watchers = watch.load_watchers(Path(args.dir))
    state = watch.load_state(Path(args.state))
    summary = watch.run(watchers, state, _post_fn(args), allow_file=args.allow_file,
                        force=args.force, only=args.only)
    if not args.dry_run or args.save_state:
        watch.save_state(Path(args.state), state)
    print(json.dumps(summary))
    return 0


def cmd_relay(args) -> int:
    from .relay import main_relay
    return main_relay(config_path=args.config)


def cmd_setup(args) -> int:
    from .github import GitHub
    number = GitHub.from_env().setup_inbox()
    print(f"inbox pull request: #{number}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"Inbox pull request: #{number}\n")
    return 0


def cmd_send(args) -> int:
    """Fire a repository_dispatch, the same call any webhook sender makes."""
    from .github import GitHub
    token = os.environ.get("INBOX_DISPATCH_TOKEN", "")
    repo = args.repo or os.environ.get("INBOX_REPO", "")
    body = {"payload": json.loads(args.payload), "source": args.source}
    if args.id:
        body["id"] = args.id
    if os.environ.get("INBOX_SECRET"):
        body["secret"] = os.environ["INBOX_SECRET"]
    GitHub(repo, token).request("POST", f"/repos/{repo}/dispatches",
                                {"event_type": args.type, "client_payload": body})
    print("sent; the relay workflow will post it in a few seconds")
    return 0


def cmd_post(args) -> int:
    event = ev.make_event(args.type, args.source, json.loads(args.payload), id=args.id)
    posted = _post_fn(args)(event)
    if posted is False:
        print("skipped duplicate")
    return 0


def cmd_parse(args) -> int:
    event = ev.from_comment(sys.stdin.read())
    if event is None:
        return 1
    print(json.dumps(event, indent=2, ensure_ascii=False))
    return 0


def cmd_check(args) -> int:
    from . import relay, watch
    watchers = watch.load_watchers(Path(args.dir))
    relay.load_config(args.config)
    enabled = sum(1 for w in watchers if w.get("enabled", True))
    print(f"ok: {len(watchers)} watchers ({enabled} enabled), inbox.yml valid")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent_inbox", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("watch")
    p.add_argument("--dir", default="watchers")
    p.add_argument("--state", default=".state/watchers.json")
    p.add_argument("--dry-run", action="store_true", help="print events, post nothing")
    p.add_argument("--save-state", action="store_true", help="save state even on a dry run")
    p.add_argument("--allow-file", action="store_true", help="allow file: URLs (local tests)")
    p.add_argument("--force", action="store_true", help="ignore each watcher's 'every'")
    p.add_argument("--only", help="run one watcher by name")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("relay")
    p.add_argument("--config", default="inbox.yml")
    p.set_defaults(fn=cmd_relay)

    sub.add_parser("setup").set_defaults(fn=cmd_setup)

    for name, fn in (("send", cmd_send), ("post", cmd_post)):
        p = sub.add_parser(name)
        p.add_argument("--type", required=True)
        p.add_argument("--source", default="cli")
        p.add_argument("--payload", default="{}", help="JSON object")
        p.add_argument("--id")
        if name == "send":
            p.add_argument("--repo", help="owner/name (or INBOX_REPO)")
        else:
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)

    sub.add_parser("parse").set_defaults(fn=cmd_parse)

    p = sub.add_parser("check")
    p.add_argument("--dir", default="watchers")
    p.add_argument("--config", default="inbox.yml")
    p.set_defaults(fn=cmd_check)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (ev.EventError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
