#!/usr/bin/env python3
"""
Autonomous Browser Agent — CLI entry point.

Usage:
    python main.py "book cheapest flight to Mumbai"
    python main.py --headless "find top Python tutorials on YouTube"
    python main.py --max-steps 50 "compare iPhone 16 prices across Amazon and eBay"
"""

import argparse
import signal
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

console = Console()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="browser-agent",
        description="Autonomous browser agent powered by Claude vision.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("goal", help="Natural-language goal for the agent.")
    p.add_argument(
        "--visible",
        action="store_true",
        default=False,
        help="Show the browser window (headless by default).",
    )
    p.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Override the maximum number of ReAct steps (default: 30).",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the Claude model (default: claude-opus-4-8).",
    )
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log verbosity (default: INFO).",
    )
    return p


def _configure_logging(level: str, logs_dir: Path) -> None:
    from loguru import logger

    logger.remove()
    # Pretty console output
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    # Structured file output
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        logs_dir / "agent_{time:YYYYMMDD}.log",
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )


def main() -> None:
    load_dotenv()  # must run before AgentConfig reads env vars

    # Convert SIGTERM to KeyboardInterrupt so the session saves state and exits cleanly.
    def _sigterm(*_: object) -> None:
        raise KeyboardInterrupt("SIGTERM")

    signal.signal(signal.SIGTERM, _sigterm)

    args = _build_parser().parse_args()

    # ── config ───────────────────────────────────────────────────────────────
    from agent.config import AgentConfig

    overrides: dict[str, Any] = {}
    if args.visible:
        overrides["headless"] = False
    if args.max_steps is not None:
        overrides["max_steps"] = args.max_steps
    if args.model is not None:
        overrides["model"] = args.model

    try:
        config = AgentConfig(**overrides)
    except Exception as exc:
        console.print(f"[bold red]Configuration error:[/] {exc}")
        sys.exit(2)

    _configure_logging(args.log_level, config.logs_dir)

    # ── header ───────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel.fit(
            Text(args.goal, style="bold cyan"),
            title="[bold white]Autonomous Browser Agent[/]",
            subtitle=f"model={config.model}  max_steps={config.max_steps}  visible={not config.headless}",
            border_style="bright_blue",
        )
    )
    console.print()

    # ── run ──────────────────────────────────────────────────────────────────
    from agent.core import AgentSession
    from agent.exceptions import MaxStepsExceededError

    session = AgentSession(config=config)

    try:
        result = session.run(goal=args.goal)
    except MaxStepsExceededError as exc:
        console.print(Rule(style="red"))
        console.print(f"[bold red]Max steps exceeded:[/] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"[bold red]Unexpected error:[/] {exc}")
        raise

    # ── result ───────────────────────────────────────────────────────────────
    console.print()
    console.print(Rule(style="green" if not result.startswith("FAILED") else "red"))

    if result.startswith("FAILED"):
        console.print(Panel(result, title="[bold red]Result[/]", border_style="red"))
        sys.exit(1)
    else:
        console.print(Panel(result, title="[bold green]Result[/]", border_style="green"))
        sys.exit(0)


if __name__ == "__main__":
    main()
