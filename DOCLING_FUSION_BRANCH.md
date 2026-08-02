# docling-fusion branch

## Where this came from — read this before deleting anything

This branch was created on **2026-08-02** from `Phase_4.3_Aziz` at commit
**`beec9ec`**.

`Phase_4.3_Aziz` is the **complete, original pipeline**. This branch is intended
to be trimmed down: files that are not needed for the docling fusion work get
deleted here. Nothing is lost by doing that — every deleted file still exists on
`Phase_4.3_Aziz`.

**If something breaks because a file is missing**, recover it with:

```
git checkout Phase_4.3_Aziz -- <path/to/file>
```

To see what this branch no longer has:

```
git diff --stat Phase_4.3_Aziz..docling-fusion
```

To browse the original tree without switching branches:

```
git ls-tree -r --name-only Phase_4.3_Aziz
```

## What this branch adds

`esg/scripts/run_docling_gold_spike.py` — a standalone spike that parses PDFs by
**fusing** two tools: docling decides the regions and their reading order,
PyMuPDF supplies the actual words. Neither does both jobs well alone. Docling's
layout model groups content correctly but flattens typography and drops some
text; PyMuPDF gets the characters exactly right but scrambles multi-column
pages.

The script is deliberately standalone. It imports nothing from `esg/src`, so
deleting pipeline modules will not break it. It does need its own virtualenv
(`venv-docling`), because docling pulls torch and transformers and must not be
mixed with the production `venv`.

## What is NOT here, and why

- `outputs/` and `reports/` are gitignored. Everything under them — the 352-page
  run, fused text, overlay images, benchmark reports — is regenerable and was
  never committed.
- `data/04_chunks/esg/` (80 MB, ~17k generated chunk files) is untracked on both
  branches. It is generated output, not source.
- Two frozen script copies (`run_docling_frozen_20260802b.py` and `...c.py`) were
  snapshots taken so parallel runs could not be disturbed by edits in progress.
  They were deleted on 2026-08-02 — they had fallen well behind the working
  script, and anything they produced is better reproduced with the current one.

## Status at the time of branching

Decided: **do not adopt docling as a replacement.** On the 40-page development
gold set it passes 13 pages against the current parser's 17. The two fail at
different things — the current parser scrambles reading order, docling loses
content — and a parser taking the better behaviour per page would reach 21/40.
That is the case for fusion, and it is bounded.

Verified: coordinate alignment between the two tools (3.8% of words fall outside
any box, and those are mostly page furniture); recall parity between table modes;
Brilliant Earth p28 empty cells 11/11 versus docling's own 8/11.

Not built: sectioning, chunking, furniture removal (regions are only *tagged*
`band=header`/`band=footer`, never dropped), row-wise table serialisation for
embedding, and the bridge into the production pipeline. Nothing here is wired
into `data/02_interim/esg_text/`.

Known unfixed: docling puts some table row boundaries in the wrong place
(LOVE p46), and roughly two thirds of the text it misses is vector art that no
text-layer parser can reach.
