"""
Tests for the generated man page (netscope.ui.man + docs/netscope.1).

The page is derived from the live parser, and the suite enforces both
halves of that promise: the rendering covers the whole CLI surface
(every command section, every option string, escaped roff-style), and
the committed docs/netscope.1 is byte-identical to a fresh render -- so
a flag added without regenerating the page fails here, not in a user's
terminal.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from netscope.ui.cli import build_arg_parser
from netscope.ui.man import render_man_page

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_PAGE = REPO_ROOT / "docs" / "netscope.1"


def _render() -> str:
    return render_man_page(build_arg_parser())


def _leaf_commands() -> list[str]:
    """Every command path the parser accepts, including service's
    nested subcommands -- the sections the page must carry."""
    parser = build_arg_parser()

    def walk(prefix: str, action: argparse._SubParsersAction) -> list[str]:
        found: list[str] = []
        for name, sub in action.choices.items():
            full = f"{prefix} {name}".strip()
            found.append(full)
            for nested in sub._actions:
                if isinstance(nested, argparse._SubParsersAction):
                    found.extend(walk(full, nested))
        return found

    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return walk("", top)


def _all_option_strings() -> set[str]:
    """Every option string anywhere in the parser tree, --help included
    (the page documents it once, in the top-level OPTIONS section)."""
    parser = build_arg_parser()
    options: set[str] = set()
    parsers: list[argparse.ArgumentParser] = [parser]
    stack: list[argparse.ArgumentParser] = [parser]
    while stack:
        current = stack.pop()
        for action in current._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    parsers.clear()
    return options


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_page_opens_with_the_standard_header():
    page = _render()
    assert page.startswith('.TH NETSCOPE 1 "" "netscope" "NetScope Manual"')
    assert page.startswith(".TH"), "the very first line must be the roff header"


def test_page_carries_every_standard_section():
    page = _render()
    for section in (".SH NAME", ".SH SYNOPSIS", ".SH DESCRIPTION", ".SH OPTIONS", ".SH COMMANDS", ".SH EXIT STATUS", ".SH FILES"):
        assert section in page, f"missing standard man-page section: {section}"


def test_page_synopsis_shows_both_invocation_forms():
    page = _render()
    assert ".B netscope" in page
    assert ".B \\-\\-version" in page, "the synopsis must show `netscope --version`"


def test_every_leaf_command_has_a_section():
    page = _render()
    for command in _leaf_commands():
        assert f".SS {command}" in page, f"command {command!r} has no .SS section"


def test_positionals_appear_as_placeholders_in_the_page():
    page = _render()
    for placeholder in ("TARGET", "SHELL", "NAME", "HOST"):
        assert f"\\fI{placeholder}\\fR" in page, f"positional placeholder {placeholder!r} missing"


# ---------------------------------------------------------------------------
# the roff surface itself
# ---------------------------------------------------------------------------


def test_every_option_string_appears_escaped():
    page = _render()
    for option in sorted(_all_option_strings()):
        escaped = option.replace("-", "\\-")
        assert escaped in page, f"option {option!r} ({escaped!r}) missing from the man page"


def test_option_dashes_are_always_escaped():
    """roff treats a bare leading dash as a control dash: after removing
    every escaped dash pair, not a single `--` may remain anywhere."""
    without_escaped = _render().replace("\\-", "")
    assert "--" not in without_escaped, "an unescaped double dash leaked into the roff source"


def test_help_text_dashes_are_escaped_too():
    """Help strings that mention flags in prose ('ignored under --json')
    must escape them like option names."""
    page = _render()
    assert "ignored under \\-\\-json" in page


# ---------------------------------------------------------------------------
# the committed copy is the render
# ---------------------------------------------------------------------------


def test_committed_page_matches_a_fresh_render():
    """docs/netscope.1 is generated, never hand-edited: it must equal a
    fresh render of the live parser, byte for byte. If this fails, run
    `make man` and commit the regenerated page."""
    assert COMMITTED_PAGE.exists(), f"{COMMITTED_PAGE.relative_to(REPO_ROOT)} is missing -- run `make man`"
    assert COMMITTED_PAGE.read_text(encoding="utf-8") == _render()
