"""CLI entry: parse startup args and run the stdio MCP server."""

from __future__ import annotations

import os

from .types import Config


def parse_args(argv: list[str] | None = None) -> Config:
    from .cli import build_parser

    parser = build_parser()
    args = parser.parse_args(argv)
    sub_key = getattr(args, 'sub_cloakbrowser_key', None)
    cloak_key = sub_key or args.cloakbrowser_key or os.environ.get('CLOAKBROWSER_LICENSE_KEY')
    country = (getattr(args, 'sub_country', None) or args.country or 'US').upper()
    return Config(country=country, cloakbrowser_key=cloak_key)


def main() -> None:
    from .cli import main as cli_main

    cli_main()


if __name__ == '__main__':
    main()
