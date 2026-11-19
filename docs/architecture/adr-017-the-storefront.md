# ADR-017: The storefront -- the repo a stranger meets first

**Status:** Accepted (v0.18, 2026-11)
**Companion:** `adr-014` (drift watch -- the feature the flagship screenshot
shows), `adr-016` (export formats -- the demo page IS one), the README's
honesty rule

## Context

The tool has been feature-complete for four releases in a row. What a
stranger (a hiring engineer, a professor, a lost network operator at 2 a.m.)
meets first is not the code -- it is the repository's front door: the README,
the demo, the release notes, the issue templates, the CI badges. That front
door can tell the truth about the product, or it can merely claim to.

Two constraints shaped every decision here:

1. **The demo must be a real artifact.** A hand-drawn mock of an evidence
   report would contradict the one thing the product guarantees (evidence,
   not vibes). So the Pages demo ships an actual `netscope report --html`
   output, generated on a locked-down VM where half the probes genuinely
   fail -- and the landing page explains the low score instead of hiding it.
   The honesty rule is the demo's selling point, not its embarrassment.
2. **Zero product code, zero new dependencies.** This release touches docs,
   community plumbing and CI only -- the tenth consecutive release without
   a new runtime dependency (`adr-006`).

## Decision

- **GitHub Pages is the storefront** (`demo/index.html`, `pages.yml`):
  one static page, the report artifact beside it, both committed as files
  (no site generator, no build step -- the workflow uploads a directory).
  The landing page answers three questions in ten seconds: what is this,
  what does the evidence look like, how do I run it myself.
- **Screenshots are rendered by the shipped code** with scripted inputs,
  openly documented in the committed generator
  (`scripts/generate_screenshots.py`): fake probes, fake windows, real
  `ui/render.py` functions -- the drift screenshot runs the actual
  `run_drift_command` against a throwaway repository. A screenshot
  pipeline that can drift from the product is a lie waiting to happen;
  the man-page rule (rendered from the live parser, so it cannot drift)
  generalized to the whole gallery.
- **Research can cite the tool** (`CITATION.cff`, version-pinned per
  release -- the GitHub "Cite this repository" button reads it).
- **Issue forms ask for evidence** (typed YAML forms: command, output,
  version) -- the bug report is to the repository what the evidence table
  is to the diagnosis: a verdict is only as good as what backs it.
- **CodeQL joins the gates** alongside ruff and mypy: static security
  analysis on push, PR and a weekly schedule -- the repo that judges a
  network's honesty also scans its own code.

## Consequences

The front door now shows the product instead of describing it: a stranger
can open the sample report and the source from the same page, in one click,
without installing anything. The demo report ages (its window is fixed at
generation time) -- acceptable: it is a specimen, not a dashboard, and the
landing page says when and how it was generated. The storefront's next
honest gap is PyPI: `pip install netscope` is still a 404, the README says
so, and the install section points at the supported source path until a
PyPI door actually opens.
