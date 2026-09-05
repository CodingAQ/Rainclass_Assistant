"""Rainclass Assistant debug entry point with a local CDP endpoint."""

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Rainclass Assistant with local CDP")
    parser.add_argument("--port", type=int, default=9222, help="local CDP port")
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="open the GUI without starting the bot",
    )
    args = parser.parse_args()

    # BrowserManager reads these values when the bot creates it.
    os.environ["RAINCLASS_DEBUG"] = "1"
    os.environ["RAINCLASS_DEBUG_PORT"] = str(args.port)

    from main import run_app

    run_app(auto_start=not args.no_auto_start)


if __name__ == "__main__":
    main()
