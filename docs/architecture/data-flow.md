# NetScope — Data Flow

**Status:** Proposed (documentation only)

This document traces one real measurement round through the system end to end, and —
because the implementation audit found this exact bug in the current code — pays
specific attention to how missing or failed measurements propagate, so that
**"not measured" is never silently treated as "healthy."**

---

## The canonical pipeline

```
Probe
  ↓
RawMeasurement
  ↓
Normalized Measurement
  ↓
Route Context
  ↓
Evidence
  ↓
Health Evaluation
  ↓
Diagnosis
  ↓
Incident
  ↓
Human Explanation
```

## Where each transformation happens

| Stage | Module | What happens |
|---|---|---|
| **Probe** | `adapters/probes/*` | A concrete adapter (`ICMPEchoProbe`, `DNSProbe`, etc.) runs against one target, calling the underlying library/OS facility. |
| **RawMeasurement** | Still inside `adapters/probes/*`, at the function boundary | The library-specific return value (an `icmplib.Host`, a `dns.resolver.Answer`, an `httpx.Response`, or a caught exception) is translated into `core.models.Measurement` — this is the only place a third-party type is allowed to exist. Success, failure, and *why* it failed (`ProbeErrorType`, `architecture-overview.md` §6) are all captured here. |
| **Normalized Measurement** | `app`, immediately after collecting a batch of `Measurement`s from one round | Unit/shape normalization across probe types (e.g. ensuring every latency value is milliseconds, every loss value is a 0–100 percentage — already consistent today per the audit's STEP 7 finding) and attachment of round-level context (timestamp, which `NetworkContext`/`Service` this round belongs to). This is a light `app`-level step, not new `core` logic — the individual adapters already produce correctly-shaped `Measurement`s. |
| **Route Context** | `core/routing.py` | For targets with an associated `RouteSnapshot` history, compute route-stability signals (churn, hop trends) that will become `Evidence` alongside the plain latency/loss `Evidence`. For targets with no traceroute data (traceroute wasn't run, or lacks privileges — ADR-003), Route Context is explicitly *absent*, not zero/empty-and-therefore-fine — see "Propagation of missing data" below. |
| **Evidence** | `core/diagnosis.py` (evidence-collection half) | `Measurement` + baseline comparison (from Intelligence) + Route Context (if present) are combined into a list of structured `Evidence` objects (`architecture-overview.md` §5) — one per relevant signal, each carrying its own `metric`, `observed_value`, `expected_value`, `deviation`, `severity`, `source`, and confidence contribution. |
| **Health Evaluation** | `core/scoring.py` (Intelligence) | Baseline-relative derived metrics and an overall experience score, feeding into Evidence like any other signal — this is the future, baseline-driven replacement for today's static-threshold `experience_score.py` (`architecture-overview.md` §10), not a new pipeline stage bolted on separately. |
| **Diagnosis** | `core/diagnosis.py` (cause-selection half, kept separate from evidence-collection per `module-boundaries.md`) | The `Evidence` list is reduced to a `Diagnosis`: a classification, confidence, supporting evidence, and — critically — an explicit statement of what wasn't tested. |
| **Incident** | `core/incidents.py` | A sequence of `Diagnosis` results across multiple rounds is watched for a sustained classification; an `Incident` opens/updates/closes based on that sustained pattern, not on any single round. |
| **Human Explanation** | `core/explanation.py` | The final `Diagnosis`/`Incident` is converted to readable text. No new reasoning happens here — if the explanation needs a fact the `Diagnosis` doesn't already contain, that's a sign the `Diagnosis`/`Evidence` model is incomplete, not a reason to add logic to Explanation. |

`app/use_cases.py` is the thread that pulls a measurement round through every stage
above in order, and is also the only place any of these intermediate outputs get
persisted (via `persistence`'s repository ports) — `core` modules never persist
anything themselves, consistent with `module-boundaries.md`.

---

## Propagation of missing/failed measurements — "NOT MEASURED ≠ HEALTHY"

This is the single most important correctness property in this document, because the
implementation audit found it violated in the current code:
`diagnosis/engine.py`'s `is_bad(None)` returns `False`, so an untested gateway
(`--gateway` not supplied) is treated identically to a healthy one, and the engine can
claim to have "ruled out" a local network issue that was never actually checked
(`test_untested_gateway_currently_behaves_as_healthy` in
`tests/test_diagnosis.py` characterizes this exact bug today).

**The rule going forward, enforced at every stage of the pipeline above:**

1. **A probe that was never invoked produces no `Measurement` at all** — there is no
   "empty" or "default" `Measurement` standing in for "we didn't check." `app`
   explicitly knows which targets/checks it *attempted* this round (it built that
   list itself, to invoke the probes), so "not attempted" is knowable at the `app`
   level without needing a sentinel value threaded through `core`.
2. **A probe that *was* invoked but failed produces a `Measurement` with
   `success=False` and a structured `ProbeErrorType`** (§6 of the overview) — this is
   a *positive* signal ("we tried and it failed this specific way"), distinct in kind
   from "not attempted." A `TIMEOUT` and a `PERMISSION_DENIED` are both `success=False`
   but mean very different things for diagnosis, which is exactly why the error type
   is structured rather than a bare string.
3. **`core/diagnosis.py`'s evidence-collection step must produce an explicit
   `Evidence` entry (or an explicit gap marker) for every target/check `app` says it
   *intended* to run this round**, distinguishing three states per target/check —
   `TESTED_HEALTHY`, `TESTED_UNHEALTHY` (with its `Evidence`), and `NOT_TESTED` — never
   collapsing the third into the first the way `is_bad(None) == False` does today.
   `NOT_TESTED` evidence is not silently dropped; it's a real entry the
   cause-selection step sees.
4. **`INSUFFICIENT_EVIDENCE` is a first-class hypothesis** (`architecture-overview.md`
   §11), not a fallback nobody reaches. If the only things tested were, say, the
   public DNS resolver and the CDN, and the gateway was never tested, a `Diagnosis`
   that concludes `ISP_ACCESS_ISSUE` must carry `NOT_TESTED` evidence for the gateway
   *alongside* whatever supporting evidence it has for the ISP hypothesis — the
   Explanation step then has the material to say "we didn't check your local network"
   rather than implying it was ruled out, mirroring the honest language pattern
   already used correctly elsewhere in this project's own audit reporting.
5. **This distinction must survive into `Incident`s too** — an `Incident`'s stored
   evidence should make it possible to later ask "was the gateway ever actually
   tested during this incident," not just "was it reported healthy."

### Concretely, what changes relative to today's code

| Today (`diagnosis/engine.py`, audited) | Required going forward |
|---|---|
| `is_bad(None)` → `False` (treated as healthy) | Absent `Measurement` → explicit `NOT_TESTED` evidence, never merged into the healthy path |
| `error: Optional[str]` discarded before reaching diagnosis | `ProbeErrorType` is itself evidence available to cause-selection |
| Three hardcoded branches (`gw_bad`/`dns_bad`/`cdn_bad`) with no concept of "untested" as a fourth state | A hypothesis space that includes `INSUFFICIENT_EVIDENCE` explicitly, and evidence lists that can name what wasn't tested |
| `ruled_out: list[str]` populated from the same `if` branch that set `likely_cause`, so "ruled out" can claim things that were never checked | `ruled_out` (or its structured `Evidence` equivalent) may only reference hypotheses actually contradicted by *tested* evidence |

No implementation of these changes happens in this task — this table exists so the
future diagnosis rewrite (`architecture-overview.md` §11, roadmap `TASK-026`) has an
explicit, reviewable checklist rather than needing to rediscover the bug's shape from
scratch.

---

## Data flow for a target with no measurements attempted at all

Worth spelling out as its own short case, since it's the simplest version of the
principle above and the one most likely to be silently mishandled: if a `Service` (or
ad hoc target) has zero enabled checks, or a whole probe category is unavailable on
the current platform (e.g. traceroute without elevated privileges, per ADR-003), the
pipeline must produce a `Diagnosis` of `INSUFFICIENT_EVIDENCE` for that target/check,
with `Evidence` explaining *why* (e.g. `PROBE_UNAVAILABLE`/`PERMISSION_DENIED` from
§6), not silently omit that target from the round's output entirely. A target that's
missing from the results should never be indistinguishable from a target that was
checked and found fine — the same principle as above, applied at the level of a whole
target rather than a single metric.
