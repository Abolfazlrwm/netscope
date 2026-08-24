# ADR-008 — Probe Adapter Implementation

**Status:** Accepted
**Implements:** the adapter side of `adr-002-probe-adapter-strategy.md` and
`adr-007-core-ports-contract.md`, connecting the existing, unmodified probe
modules to `core.ports.Probe` for the first time (TASK-007).

## Why adapters exist

`core.ports.Probe` (ADR-007) describes *what* a probe does without describing
*how*. Something still has to actually connect that shape to real measurement
code. That something is `netscope.adapters.probes.*`: one small class per
probe type (`ICMPProbeAdapter`, `DNSProbeAdapter`, `HTTPProbeAdapter`), each
satisfying `Probe` by exposing a `probe_type` attribute and a
`run(target, **options) -> RawMeasurement` method that delegates to the
existing module-level function (`icmp_probe.ping`, `dns_probe.resolve`,
`http_probe.fetch`) and returns exactly what it returns.

## Why core does not know adapters exist

`core.ports.py` (unchanged by this task, re-verified by its own regression
test) still imports nothing beyond `typing` and `netscope.core.models`. The
dependency direction is one-way: `adapters` imports `core` (to use `Probe`,
`ProbeType`, `RawMeasurement`), never the reverse. This is what makes it true
that adding a new probe type, or swapping which library an adapter wraps,
touches only `adapters` — `core` is unaffected either way, and stays fully
testable offline with no network library installed, exactly as
`architecture-overview.md` §16 claims.

## How existing probes are reused

Nothing in `netscope.probes.icmp_probe`, `dns_probe`, or `http_probe` was
changed. Each adapter is a thin, few-line pass-through:

```python
class ICMPProbeAdapter:
    probe_type = ProbeType.ICMP
    def run(self, target: str, **options: Any) -> RawMeasurement:
        return icmp_probe.ping(target, **options)
```

`**options` forwards whatever caller-supplied keyword arguments exist
straight to the underlying function's own parameters (`count`, `timeout`,
`privileged` for ICMP; `record_type`, `resolver_ip`, `timeout` for DNS;
`timeout` for HTTP) — the adapter does not need to know or repeat that
parameter list, and the underlying function's own defaults still apply when
no options are given, confirmed by
`test_adapters_work_with_no_extra_options_using_underlying_defaults`.

## Why we avoid rewriting working code

The implementation audit (`implementation-audit.md`) already evaluated
`icmp_probe.py` and `dns_probe.py` as **KEEP** — correct library usage,
correct error containment, no structural problem. Rewriting them now, inside
a task whose only job is to add an adapter layer, would mix an unrelated
change into this one and risk regressing already-verified behavior for no
benefit: the adapter's whole purpose is to make the *existing* implementation
satisfy a *new* contract, not to replace the implementation. This is also why
the adapters add no error handling of their own — the existing probe
functions already catch their own exceptions and return a `success=False`
`RawMeasurement` rather than raising (per the implementation audit's STEP 7
finding); an adapter that added a second try/except layer on top would risk
masking a genuine, unexpected programming error differently than today's code
does. `test_icmp_adapter_does_not_add_its_own_error_handling` asserts this
directly: an unexpected exception from the underlying function propagates
through the adapter unchanged, it is not caught and reinterpreted here.

(`http_probe.py`'s known TTFB docstring/implementation mismatch, flagged as
**MODIFY** by the audit, is intentionally left untouched by this task too —
that fix belongs to `future-roadmap.md` TASK-018, not to an adapter task.)

## Consequences

- `netscope.probes.*` is unchanged and continues to work exactly as before,
  including for any future direct callers.
- `netscope.adapters.probes.*` is the new, `Probe`-conforming entry point that
  `app`'s future composition root will use instead.
- Both can coexist during the migration; nothing about this task requires the
  old call sites to be updated yet, and none were.
