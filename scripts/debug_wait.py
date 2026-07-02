"""Start a debugpy listener for attach-style local debugging."""

from __future__ import annotations

import argparse

import debugpy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5678, type=int)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    debugpy.listen((args.host, args.port))
    print(f"debugpy listening on {args.host}:{args.port}")
    if args.wait:
        print("waiting for debugger attach...")
        debugpy.wait_for_client()
        print("debugger attached")


if __name__ == "__main__":
    main()
