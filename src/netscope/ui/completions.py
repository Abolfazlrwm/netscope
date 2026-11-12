"""
netscope.ui.completions

Hand-written shell completion scripts for the netscope CLI, shipped as
plain data and printed verbatim by `netscope completions <shell>`.

Why hand-written rather than mechanically generated: each script carries
its shell's own install path and idiomatic helpers (bash's `complete`
registration, zsh's `#compdef` header, fish's `__fish_use_subcommand`
guards), and the scripts deliberately stay static -- a completion that
runs `netscope` on every TAB press would be slower than the tool it
completes. The trade-off is drift risk, so the test suite pins every
script to the real CLI surface: every command name and every option of
build_arg_parser() must appear in each script, and a future flag without
a completion fails the build instead of shipping half-finished tabs.

The scripts know the full v0.11 surface:

- diagnose check route history monitor speed report service completions tui
- the `service` sub-subcommands (add / list / remove / check)
- every per-command option, `--version`, and the five --checks values
"""

from __future__ import annotations

# The shells netscope ships completions for, in a stable order.
SUPPORTED_SHELLS = ("bash", "zsh", "fish")


def completion_shells() -> tuple[str, ...]:
    """Returns the shells with shipped completion scripts, in the
    stable order they are documented in."""
    return SUPPORTED_SHELLS


def completion_script(shell: str) -> str:
    """Returns the completion script for `shell`, verbatim -- callers
    print it to stdout unstyled, so it can be redirected straight into
    a completion file. An unknown shell raises ValueError with the
    supported list, which the CLI turns into its standard usage error."""
    scripts = {
        "bash": BASH_COMPLETION_SCRIPT,
        "zsh": ZSH_COMPLETION_SCRIPT,
        "fish": FISH_COMPLETION_SCRIPT,
    }
    try:
        return scripts[shell]
    except KeyError:
        supported = ", ".join(SUPPORTED_SHELLS)
        raise ValueError(f"unknown shell {shell!r} -- supported shells: {supported}") from None


BASH_COMPLETION_SCRIPT = """\
# netscope bash completion
#
# Install once:
#   mkdir -p ~/.local/share/bash-completion/completions
#   netscope completions bash > ~/.local/share/bash-completion/completions/netscope
# (or try it live first: source <(netscope completions bash))

_netscope() {
    local cur prev cmd
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    cmd="${COMP_WORDS[1]}"

    local commands="diagnose check route history monitor speed report service completions tui"
    local service_commands="add list remove check"
    local checks="icmp dns tcp tls http"

    # right after the program name: the command list (--version is the
    # one flag that lives at the top level, before any subcommand)
    if [[ $COMP_CWORD -eq 1 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "--version" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        fi
        return 0
    fi

    # `netscope service <TAB>` and `netscope completions <TAB>`
    if [[ "$prev" == "service" ]]; then
        COMPREPLY=( $(compgen -W "$service_commands" -- "$cur") )
        return 0
    fi
    if [[ "$prev" == "completions" ]]; then
        COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
        return 0
    fi

    # the shared --checks option (check / service add): complete the
    # last segment of a comma-separated list, so `icmp,d<TAB>` works
    if [[ "$prev" == "--checks" ]]; then
        local last="${cur##*,}"
        local prefix="${cur%${last}}"
        COMPREPLY=( $(compgen -W "$checks" -P "$prefix" -- "$last") )
        return 0
    fi

    # flag names, per command
    if [[ "$cur" == -* ]]; then
        local opts=""
        case "$cmd" in
            diagnose)    opts="--gateway --json --verbose" ;;
            check)       opts="--checks --json --verbose" ;;
            route)       opts="--no-resolve --json --verbose" ;;
            history)     opts="--limit --json --verbose" ;;
            monitor)     opts="--interval --rounds --gateway --verbose" ;;
            speed)       opts="--url --seconds --json --verbose" ;;
            report)      opts="--days --target --out --html --json --verbose" ;;
            service)     opts="--checks --json --verbose" ;;
            completions) opts="--verbose" ;;
        esac
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
        return 0
    fi

    # everything else is a hostname, a name or a number: fall back to
    # the shell's default (filenames also cover --out/--html/--url)
    return 0
}
complete -o default -F _netscope netscope
"""

ZSH_COMPLETION_SCRIPT = """\
#compdef netscope
# netscope zsh completion
#
# Install once:
#   mkdir -p ~/.zfunc
#   netscope completions zsh > ~/.zfunc/_netscope
# then in .zshrc, BEFORE compinit runs:
#   fpath=(~/.zfunc $fpath)
# (or try it live first: source <(netscope completions zsh))

_netscope() {
    local -a commands
    commands=(
        'diagnose:run a diagnostic round (gateway/DNS/CDN checks) and report the result'
        'check:probe one target with any subset of the service checks'
        'route:trace the route to a target and report route stability'
        'history:show recent measurement rounds from the local log'
        'monitor:run continuous measurement rounds with incident detection'
        'speed:measure download throughput (median Mbps over a sampling window)'
        'report:export a self-contained evidence report from the measurement log'
        'service:manage the saved service list'
        'completions:print a shell completion script for netscope'
        'tui:launch the live terminal dashboard'
    )

    _arguments -C \\
        '--version[show the version and exit]' \\
        '(-h --help)'{-h,--help}'[show the help and exit]' \\
        '1:command:->command' \\
        '*::argument:->argument'

    case $state in
        command)
            _describe -t commands 'netscope command' commands
            ;;
        argument)
            case $words[1] in
                diagnose)
                    _arguments \\
                        '--gateway[gateway to probe: an IP, "auto" (default) or "none"]:value:' \\
                        '--json[output machine-readable JSON instead of styled text]' \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
                check)
                    _arguments \\
                        '1:target:_host' \\
                        '--checks[comma-separated subset of checks]:checks:_values -s , icmp dns tcp tls http' \\
                        '--json[output machine-readable JSON instead of styled text]' \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
                route)
                    _arguments \\
                        '1:target:_host' \\
                        '--no-resolve[skip reverse-DNS hostname resolution for hops]' \\
                        '--json[output machine-readable JSON instead of styled text]' \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
                history)
                    _arguments \\
                        '--limit[maximum number of rounds to show]:limit:' \\
                        '--json[output machine-readable JSON instead of styled text]' \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
                monitor)
                    _arguments \\
                        '--interval[seconds between rounds]:interval:' \\
                        '--rounds[number of rounds before stopping]:rounds:' \\
                        '--gateway[gateway to probe: an IP, "auto" or "none"]:value:' \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
                speed)
                    _arguments \\
                        '--url[download URL to measure]:url:_urls' \\
                        '--seconds[measurement window in seconds]:seconds:' \\
                        '--json[output machine-readable JSON instead of styled text]' \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
                report)
                    _arguments \\
                        '--days[window length in days]:days:' \\
                        '--target[restrict the report to one target]:target:' \\
                        '--out[write the markdown report to a file]:file:_files' \\
                        '--html[write a self-contained HTML report to a file]:file:_files' \\
                        '--json[output machine-readable JSON instead of markdown]' \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
                service)
                    local -a service_commands
                    service_commands=(
                        'add:save (or update) a service in the curated list'
                        'list:list the saved services'
                        'remove:remove a service from the saved list'
                        'check:run a saved service check now and report the result'
                    )
                    _arguments -C \\
                        '1:service command:->service_command' \\
                        '*::service argument:->service_argument'
                    case $state in
                        service_command)
                            _describe -t service-commands 'service command' service_commands
                            ;;
                        service_argument)
                            case $words[1] in
                                add)
                                    _arguments \\
                                        '1:name:' \\
                                        '2:host:_host' \\
                                        '--checks[comma-separated subset of checks]:checks:_values -s , icmp dns tcp tls http'
                                    ;;
                                list)
                                    _arguments \\
                                        '--json[output machine-readable JSON instead of styled text]'
                                    ;;
                                remove)
                                    _arguments '1:name:'
                                    ;;
                                check)
                                    _arguments \\
                                        '1:name:' \\
                                        '--json[output machine-readable JSON instead of styled text]'
                                    ;;
                            esac
                            ;;
                    esac
                    ;;
                completions)
                    _arguments '1:shell:(bash zsh fish)'
                    ;;
                tui)
                    _arguments \\
                        '(-v --verbose)'{-v,--verbose}'[enable verbose (INFO-level) logging]'
                    ;;
            esac
            ;;
    esac
}

_netscope "$@"
"""

FISH_COMPLETION_SCRIPT = """\
# netscope fish completion
#
# Install once:
#   mkdir -p ~/.config/fish/completions
#   netscope completions fish > ~/.config/fish/completions/netscope.fish
# (or try it live first: netscope completions fish | source)

# commands
complete -c netscope -n "__fish_use_subcommand" -a "diagnose" -d "run a diagnostic round (gateway/DNS/CDN checks) and report the result"
complete -c netscope -n "__fish_use_subcommand" -a "check" -d "probe one target with any subset of the service checks"
complete -c netscope -n "__fish_use_subcommand" -a "route" -d "trace the route to a target and report route stability"
complete -c netscope -n "__fish_use_subcommand" -a "history" -d "show recent measurement rounds from the local log"
complete -c netscope -n "__fish_use_subcommand" -a "monitor" -d "run continuous measurement rounds with incident detection"
complete -c netscope -n "__fish_use_subcommand" -a "speed" -d "measure download throughput (median Mbps over a sampling window)"
complete -c netscope -n "__fish_use_subcommand" -a "report" -d "export a self-contained evidence report from the measurement log"
complete -c netscope -n "__fish_use_subcommand" -a "service" -d "manage the saved service list"
complete -c netscope -n "__fish_use_subcommand" -a "completions" -d "print a shell completion script for netscope"
complete -c netscope -n "__fish_use_subcommand" -a "tui" -d "launch the live terminal dashboard"

# the service sub-subcommands, then their own flags
complete -c netscope -n "__fish_seen_subcommand_from service; and not __fish_seen_subcommand_from add list remove check" -a "add" -d "save (or update) a service in the curated list"
complete -c netscope -n "__fish_seen_subcommand_from service; and not __fish_seen_subcommand_from add list remove check" -a "list" -d "list the saved services"
complete -c netscope -n "__fish_seen_subcommand_from service; and not __fish_seen_subcommand_from add list remove check" -a "remove" -d "remove a service from the saved list"
complete -c netscope -n "__fish_seen_subcommand_from service; and not __fish_seen_subcommand_from add list remove check" -a "check" -d "run a saved service check now and report the result"
complete -c netscope -n "__fish_seen_subcommand_from service; and __fish_seen_subcommand_from add" -l checks -d "comma-separated subset of checks" -x -a "icmp dns tcp tls http"
complete -c netscope -n "__fish_seen_subcommand_from service; and __fish_seen_subcommand_from list" -l json -d "output machine-readable JSON instead of styled text"
complete -c netscope -n "__fish_seen_subcommand_from service; and __fish_seen_subcommand_from check" -l json -d "output machine-readable JSON instead of styled text"

# per-command flags
complete -c netscope -n "__fish_seen_subcommand_from diagnose" -l gateway -d "gateway to probe: an IP, auto (default) or none" -x
complete -c netscope -n "__fish_seen_subcommand_from diagnose" -l json -d "output machine-readable JSON instead of styled text"
complete -c netscope -n "__fish_seen_subcommand_from check" -l checks -d "comma-separated subset of checks" -x -a "icmp dns tcp tls http"
complete -c netscope -n "__fish_seen_subcommand_from check" -l json -d "output machine-readable JSON instead of styled text"
complete -c netscope -n "__fish_seen_subcommand_from route" -l no-resolve -d "skip reverse-DNS hostname resolution for hops"
complete -c netscope -n "__fish_seen_subcommand_from route" -l json -d "output machine-readable JSON instead of styled text"
complete -c netscope -n "__fish_seen_subcommand_from history" -l limit -d "maximum number of rounds to show" -x
complete -c netscope -n "__fish_seen_subcommand_from history" -l json -d "output machine-readable JSON instead of styled text"
complete -c netscope -n "__fish_seen_subcommand_from monitor" -l interval -d "seconds between rounds" -x
complete -c netscope -n "__fish_seen_subcommand_from monitor" -l rounds -d "number of rounds before stopping" -x
complete -c netscope -n "__fish_seen_subcommand_from monitor" -l gateway -d "gateway to probe: an IP, auto or none" -x
complete -c netscope -n "__fish_seen_subcommand_from speed" -l url -d "download URL to measure" -x
complete -c netscope -n "__fish_seen_subcommand_from speed" -l seconds -d "measurement window in seconds" -x
complete -c netscope -n "__fish_seen_subcommand_from speed" -l json -d "output machine-readable JSON instead of styled text"
complete -c netscope -n "__fish_seen_subcommand_from report" -l days -d "window length in days" -x
complete -c netscope -n "__fish_seen_subcommand_from report" -l target -d "restrict the report to one target" -x
complete -c netscope -n "__fish_seen_subcommand_from report" -l out -d "write the markdown report to a file" -rF
complete -c netscope -n "__fish_seen_subcommand_from report" -l html -d "write a self-contained HTML report to a file" -rF
complete -c netscope -n "__fish_seen_subcommand_from report" -l json -d "output machine-readable JSON instead of markdown"
complete -c netscope -n "__fish_seen_subcommand_from completions" -a "bash" -d "bash completion script"
complete -c netscope -n "__fish_seen_subcommand_from completions" -a "zsh" -d "zsh completion script"
complete -c netscope -n "__fish_seen_subcommand_from completions" -a "fish" -d "fish completion script"

# shared flags
complete -c netscope -l version -d "show the version and exit"
complete -c netscope -l verbose -s v -d "enable verbose (INFO-level) logging"
"""
