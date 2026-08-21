"""CLI entry: parse startup args and run the stdio MCP server."""

from __future__ import annotations

import argparse
import os

from .types import Config


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog='car-deals-mcp',
        description='MCP server for searching car deals (US + UK) and UK MOT history.',
    )
    parser.add_argument(
        '--country',
        default='US',
        choices=['US', 'UK'],
        help='Default country the server operates in (per-call country arg overrides).',
    )
    parser.add_argument(
        '--cloakbrowser-key',
        default=os.environ.get('CLOAKBROWSER_LICENSE_KEY'),
        help='CloakBrowser license key (env: CLOAKBROWSER_LICENSE_KEY). CLI arg wins.',
    )
    # Log-level flags mirror the JS `--verbose` / `--trace`. The logger reads
    # these from argv itself, but declaring them here keeps --help honest and
    # prevents argparse from erroring on unknown args a client might pass.
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug log level.')
    parser.add_argument('--trace', action='store_true', help='Trace log level (verbosest).')
    args = parser.parse_args(argv)
    return Config(country=args.country, cloakbrowser_key=args.cloakbrowser_key)


def main() -> None:
    config = parse_args()
    # Imported here so `parse_args` stays importable without pulling the MCP
    # SDK or cloakbrowser (tests parse args without running the server).
    from .server import run

    run(config)


if __name__ == '__main__':
    main()
