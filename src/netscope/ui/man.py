r"""
netscope.ui.man

Renders the netscope man page (roff) straight from the live argparse
parser -- one surface, one font. The page cannot drift from the CLI
because it IS the CLI, read back: `scripts/gen_man.py` writes the
committed docs/netscope.1 from build_arg_parser(), and the test suite
re-renders and compares, so a flag added without regenerating the page
fails the build.

roff ground rules the renderer obeys: every literal dash in option
names and help text is escaped (`\-\-json`) because man renderers treat
a bare leading dash as a control dash; the page is plain UTF-8; and
beyond bold/italic there are no markup tricks. The date field is left
empty on purpose -- a generated page must be deterministic, or the
committed copy and a fresh render would disagree about today.
"""

from __future__ import annotations

import argparse


def _roff(text: str) -> str:
    """Escapes a plain-text fragment for roff: backslashes are doubled
    first, then every literal '-' becomes '\\-', since an unescaped
    dash in a .B/.I line can render as a control dash and corrupt
    option names like --no-resolve."""
    return text.replace("\\", "\\\\").replace("-", "\\-")


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _command_helps(action: argparse._SubParsersAction) -> dict[str, str]:
    """Per-command help strings, read back from the subparsers action's
    own choice display (its dest holds the bare command name)."""
    helps: dict[str, str] = {}
    for choice in getattr(action, "_choices_actions", []):  # argparse private; stable across 3.11-3.13
        helps[choice.dest] = choice.help or ""
    return helps


def _split_actions(sub: argparse.ArgumentParser) -> tuple[list[argparse.Action], list[argparse.Action]]:
    """Splits a subcommand's actions into positionals and options,
    dropping the auto-added help action (it is documented once, in the
    top-level OPTIONS section)."""
    positionals: list[argparse.Action] = []
    options: list[argparse.Action] = []
    for action in sub._actions:
        if action.dest == "help":
            continue
        if action.option_strings:
            options.append(action)
        else:
            positionals.append(action)
    return positionals, options


def _placeholder(action: argparse.Action) -> str | None:
    """The value placeholder for an option that takes one (GATEWAY,
    CHECKS, FILE...), or None for a pure flag."""
    if action.nargs == 0:
        return None
    return str(action.metavar or action.dest.upper())


def _render_command(lines: list[str], name: str, sub: argparse.ArgumentParser, help_text: str, prefix: str = "") -> None:
    """One COMMANDS section entry: the command's help, a synopsis line,
    and a .TP block per option. Nested subcommands (service's) render
    as their own full sections right after their parent."""
    full = f"{prefix} {name}".strip()
    lines.append(f".SS {_roff(full)}")
    if help_text:
        lines.append(_roff(help_text))
        lines.append(".P")

    positionals, options = _split_actions(sub)
    synopsis = [f"\\fB{_roff(sub.prog)}\\fR"]
    for action in positionals:
        metavar = _placeholder(action) or action.dest.upper()
        synopsis.append(f"\\fI{_roff(metavar)}\\fR")
    for action in options:
        flags = _roff(action.option_strings[-1])
        placeholder = _placeholder(action)
        if placeholder:
            synopsis.append(f"[{flags} \\fI{_roff(placeholder)}\\fR]")
        else:
            synopsis.append(f"[{flags}]")
    lines.append(" ".join(synopsis))
    lines.append(".P")

    for action in options:
        all_flags = _roff(", ".join(action.option_strings))
        placeholder = _placeholder(action)
        help_line = _roff(action.help or "")
        if placeholder:
            lines.append(f".TP\n.B {all_flags} \\fI{_roff(placeholder)}\\fR\n{help_line}")
        else:
            lines.append(f".TP\n.B {all_flags}\n{help_line}")

    nested = _subparsers_action(sub)
    if nested:
        nested_helps = _command_helps(nested)
        for nested_name, nested_sub in nested.choices.items():
            _render_command(lines, nested_name, nested_sub, nested_helps.get(nested_name, ""), prefix=f"{prefix} {name}")


def render_man_page(parser: argparse.ArgumentParser) -> str:
    """Renders the complete man page for netscope's CLI as roff text,
    derived entirely from the given parser plus the CLI's fixed facts
    (exit codes, the ~/.netscope files). Raises ValueError if the
    parser carries no subcommands -- there would be nothing to render."""
    subparsers = _subparsers_action(parser)
    if subparsers is None:
        raise ValueError("the parser has no subcommands; nothing to render")

    lines: list[str] = [
        '.TH NETSCOPE 1 "" "netscope" "NetScope Manual"',
        ".SH NAME",
        f"{parser.prog} \\- {_roff(parser.description or '')}",
        ".SH SYNOPSIS",
        ".B netscope",
        '.IR command " [ " options " ]"',
        ".br",
        ".B netscope",
        ".B \\-\\-version",
        ".SH DESCRIPTION",
        _roff(parser.description or ""),
        ".SH OPTIONS",
    ]

    for action in parser._actions:
        if not action.option_strings:
            continue
        all_flags = _roff(", ".join(action.option_strings))
        lines.append(f".TP\n.B {all_flags}\n{_roff(action.help or '')}")

    lines.append(".SH COMMANDS")
    command_helps = _command_helps(subparsers)
    for name, sub in subparsers.choices.items():
        _render_command(lines, name, sub, command_helps.get(name, ""))

    lines.extend(
        [
            ".SH EXIT STATUS",
            ".TP\n.B 0\nsuccess; the evidence is printed even when the diagnosis reports a problem",
            ".TP\n.B 1\nruntime failure, e.g. `netscope tui` without its extra installed",
            ".TP\n.B 2\nusage error (unknown command, bad option value)",
            ".SH FILES",
            ".TP\n.I ~/.netscope/config.toml\noptional user configuration",
            ".TP\n.I ~/.netscope/netscope.db\nlocal SQLite log: measurements, baselines, route history, saved services",
            ".TP\n.I ~/.netscope/ipasn.dat\noptional offline ASN database; enables AS numbers in `netscope route`",
        ]
    )
    return "\n".join(lines) + "\n"
