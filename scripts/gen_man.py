#!/usr/bin/env python3
"""
Regenerates docs/netscope.1 from the live argparse parser.

The committed man page is generated, never hand-edited: change the CLI
(or the page's fixed sections in netscope.ui.man), then run `make man`
and commit the regenerated page. The test suite re-renders and
compares, so a stale page fails the build instead of shipping a man
page that lies about a flag.

    python scripts/gen_man.py          # rewrite docs/netscope.1
"""

from __future__ import annotations

from pathlib import Path

from netscope.ui.cli import build_arg_parser
from netscope.ui.man import render_man_page

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "netscope.1"


def main() -> None:
    page = render_man_page(build_arg_parser())
    TARGET.write_text(page, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(page.splitlines())} lines)")


if __name__ == "__main__":
    main()
