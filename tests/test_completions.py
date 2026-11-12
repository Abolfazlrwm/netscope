"""
Tests for netscope.ui.completions -- the shipped shell completion scripts.

The scripts are hand-written on purpose (each carries its shell's own
install path and idiomatic helpers), so the suite pins them to the real
CLI surface instead of trusting three free-floating strings: every
command name and every option of build_arg_parser() must appear in each
script. A future command or flag without a completion breaks the build
here rather than shipping silently unfinished tab completion.
"""

from __future__ import annotations

import argparse

import pytest

from netscope.ui.cli import build_arg_parser, run_completions_command
from netscope.ui.completions import completion_script, completion_shells

# ---------------------------------------------------------------------------
# the real CLI surface, read back from the parser
# ---------------------------------------------------------------------------


def _subcommand_names() -> list[str]:
    action = next(a for a in build_arg_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return sorted(action.choices)


def _option_strings() -> set[str]:
    """Every option string the parser can accept, including --version
    at the top level and every per-subcommand flag (--help excluded:
    argparse provides it for free and no hand-written script lists it)."""
    parser = build_arg_parser()
    options: set[str] = set()
    for action in parser._actions:
        options.update(action.option_strings)
    top_level = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for sub in top_level.choices.values():
        for action in sub._actions:
            options.update(action.option_strings)
    options.discard("-h")
    options.discard("--help")
    return options


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------


def test_supported_shells_are_bash_zsh_and_fish():
    assert completion_shells() == ("bash", "zsh", "fish")


def test_unknown_shell_is_rejected_with_the_supported_list():
    with pytest.raises(ValueError) as exc:
        completion_script("powershell")
    assert "bash, zsh, fish" in str(exc.value)


def test_each_shell_returns_its_own_script():
    scripts = {shell: completion_script(shell) for shell in completion_shells()}
    assert len(set(scripts.values())) == 3, "three shells must ship three distinct scripts"
    for shell, script in scripts.items():
        first_line = script.splitlines()[0]
        assert first_line.startswith("#") and "netscope" in first_line, f"the {shell} script must open with a netscope header"
        assert shell in script, f"the {shell} script must mention its own shell (install instructions)"


def test_every_script_carries_its_shell_install_instructions():
    install_paths = {
        "bash": "~/.local/share/bash-completion/completions/netscope",
        "zsh": "~/.zfunc/_netscope",
        "fish": "~/.config/fish/completions/netscope.fish",
    }
    for shell, path in install_paths.items():
        script = completion_script(shell)
        assert "Install once:" in script
        assert path in script, f"the {shell} script must name its real install path ({path})"


# ---------------------------------------------------------------------------
# the scripts stay pinned to the parser
# ---------------------------------------------------------------------------


def test_every_command_appears_in_every_script():
    commands = _subcommand_names()
    assert {"diagnose", "check", "route", "history", "monitor", "speed", "report", "service", "tui"} <= set(commands), (
        "sanity: introspection found the real command surface"
    )
    for shell in completion_shells():
        script = completion_script(shell)
        for command in commands:
            assert command in script, f"command {command!r} is missing from the {shell} script"


def _option_spelled_as(shell: str, option: str) -> str:
    """fish spells long options as `-l name` and short options as
    `-s letter` rather than argparse's `--name` / `-letter`; the other
    two shells use the double-dash spelling verbatim."""
    if shell != "fish":
        return option
    if option.startswith("--"):
        return f"-l {option[2:]}"
    if option.startswith("-"):
        return f"-s {option[1:]}"
    return option


def test_every_option_appears_in_every_script():
    options = _option_strings()
    assert "--version" in options and "--checks" in options, "sanity: the expected surface is being read"
    for shell in completion_shells():
        script = completion_script(shell)
        for option in sorted(options):
            expected = _option_spelled_as(shell, option)
            assert expected in script, f"option {option!r} ({expected!r}) is missing from the {shell} script"


def test_the_checks_values_are_completable_in_every_script():
    for shell in completion_shells():
        script = completion_script(shell)
        for value in ("icmp", "dns", "tcp", "tls", "http"):
            assert value in script, f"the {shell} script cannot complete --checks value {value!r}"


# ---------------------------------------------------------------------------
# each script stays idiomatic to its own shell
# ---------------------------------------------------------------------------


def test_bash_script_registers_a_completion_function():
    script = completion_script("bash")
    assert "_netscope()" in script
    assert "complete -o default -F _netscope netscope" in script


def test_zsh_script_is_a_compdef_file():
    script = completion_script("zsh")
    assert script.startswith("#compdef netscope")
    assert "_arguments" in script
    assert "_describe" in script


def test_fish_script_speaks_complete_invocations():
    script = completion_script("fish")
    assert script.count("complete -c netscope") >= 25, "fish completes through one `complete` line per item"
    assert "__fish_use_subcommand" in script


# ---------------------------------------------------------------------------
# the `completions` command: parser + runner
# ---------------------------------------------------------------------------


def test_parser_completions_subcommand_takes_a_shell():
    args = build_arg_parser().parse_args(["completions", "bash"])
    assert args.command == "completions"
    assert args.shell == "bash"


def test_parser_completions_accepts_all_three_supported_shells():
    for shell in completion_shells():
        args = build_arg_parser().parse_args(["completions", shell])
        assert args.shell == shell


def test_parser_completions_rejects_an_unknown_shell(capsys):
    with pytest.raises(SystemExit) as exc:
        build_arg_parser().parse_args(["completions", "powershell"])
    assert exc.value.code == 2
    assert "powershell" in capsys.readouterr().err


def test_runner_prints_the_script_verbatim_and_exits_cleanly(capsys):
    """The command's whole contract: stdout carries exactly the shipped
    script -- byte for byte, so a redirect produces a working file."""
    assert run_completions_command("zsh") == 0
    assert capsys.readouterr().out == completion_script("zsh")


def test_runner_output_carries_no_ansi_styling(capsys):
    """Completion output is piped into files and sourced by shells; a
    single rich-styled escape sequence would corrupt it."""
    run_completions_command("bash")
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert out.endswith("complete -o default -F _netscope netscope\n")
