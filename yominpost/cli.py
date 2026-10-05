"""YominPost command line.

  yominpost                      # same as `yominpost serve`: start the web app on http://127.0.0.1:8300
  yominpost serve --port 8300 --open
  yominpost run --brand "Acme" --source "We help founders ship content faster" \\
      --platform x --platform instagram --platform tiktok --topics 3 [--json]
  yominpost mcp                  # MCP server on stdio (same as `yominpost-mcp`)

Zero config: no API keys needed (offline template engine by default), data lives in ~/.yominpost/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__


def _cmd_run(args: argparse.Namespace) -> int:
    from .orchestrator import run as run_pipeline
    from .schemas import BrandSource, PipelineRequest, Platform

    try:
        platforms = [Platform(p) for p in (args.platform or ["x"])]
    except ValueError as e:
        print(f"error: {e}. Choose from: {', '.join(p.value for p in Platform)}", file=sys.stderr)
        return 2
    req = PipelineRequest(
        brand_name=args.brand,
        sources=[BrandSource(kind="note", text=s) for s in (args.source or [])],
        raw_trends=args.trend or [],
        platforms=platforms,
        num_topics=args.topics,
        language=args.language,
    )
    res = run_pipeline(req, persist=not args.no_persist)
    if args.json:
        print(res.model_dump_json(indent=2))
        return 0
    print(f"run_id={res.run_id}  provider={res.provider}  packages={len(res.packages)}\n")
    for pk in res.packages:
        d = pk.format_decision
        q = pk.qa
        print(f"● [{d.platform.value}] {d.format.value}  QA={q.overall} "
              f"pass={q.passed} revised={q.revised}")
        print(f"  hook: {pk.copy.hook}")
        if pk.visual:
            print(f"  poster: {pk.visual.svg_path}")
        if pk.video:
            print(f"  video: {pk.video.total_duration_sec}s  srt: {pk.video.srt_path}")
        print()
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import DATA_DIR

    url = f"http://{args.host}:{args.port}"
    print(f"YominPost {__version__} → {url}   (data: {DATA_DIR})", file=sys.stderr)
    if args.open:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("yominpost.web.app:app", host=args.host, port=args.port, log_level="info")
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="yominpost", description="Self-hosted AI social media studio: "
                                 "generate, review, schedule and publish posts from one place.")
    ap.add_argument("--version", action="version", version=f"yominpost {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the web app (default)")
    s.add_argument("--host", default=os.environ.get("YOMINPOST_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8300")))
    s.add_argument("--open", action="store_true", help="open the browser after start")
    s.set_defaults(func=_cmd_serve)

    r = sub.add_parser("run", help="run the 8-slot content pipeline once and print the result")
    r.add_argument("--brand", default="YominPost")
    r.add_argument("--source", action="append", help="brand material / notes (repeatable)")
    r.add_argument("--trend", action="append", help="trend signal (repeatable)")
    r.add_argument("--platform", action="append",
                   help="target platform: x, linkedin, instagram, tiktok, youtube, xiaohongshu, wechat (repeatable)")
    r.add_argument("--topics", type=int, default=3)
    r.add_argument("--language", default="auto", help="copy language: auto (follow the material), en, zh")
    r.add_argument("--json", action="store_true", help="print the full result as JSON")
    r.add_argument("--no-persist", action="store_true")
    r.set_defaults(func=_cmd_run)

    m = sub.add_parser("mcp", help="run the MCP server on stdio")
    m.set_defaults(func=_cmd_mcp)
    return ap


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"serve", "run", "mcp", "-h", "--help", "--version"}:
        argv = ["serve", *argv]          # bare `yominpost [--port N]` starts the web app
    args = build_parser().parse_args(argv)
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
