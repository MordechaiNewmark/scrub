# Scrub

A small, **fully local** tool that finds private information in a document
and either blacks it out, replaces it with realistic fake data, or labels it
with a generic type tag like `(name)` or `(ssn)`.

Built for reviewing sensitive documents (e.g. legal intake, retainer letters,
medical records attached to a case) where the original must never leave the
computer.

- **Nothing is uploaded anywhere.** Detection runs on your machine. The
  original document is held in memory only and discarded after 30 minutes.
- Handles **.txt, .docx, and .pdf**, writing the cleaned result back in the
  same format with formatting preserved.
- Every found item is shown highlighted in the document for review; you choose
  *Replace with fake*, *Black out*, *Type tag*, or *Keep* — globally, by
  entity type, or per item — before downloading.

## What it detects

- **Names** — `James Lin`, `Atty. Mendoza`, bare last names attached by context
- **SSN** — `123-45-6789`, `123 45 6789`, `123.45.6789`
- **Email** — `james.lin@example.com`
- **Phone** — `(415) 555-0142`, `415-555-7733`
- **Credit card** — `4111 1111 1111 1111`
- **Street address** — `482 Cedar Lane, Apt 7B`, `742 Evergreen Terrace`
- **City / state / place** — `Brooklyn`, `NY 11201`
- **Date** — `March 14, 2024`, `04/01/2024`
- **Case / docket #** — `1:24-cv-08891`, `Index No. 654321/2024`, `CV-2024-0817`
- **Medical IDs** — MRN, ICD-10 (`M54.5`), CPT, NPI — surfaced only with medical context nearby
- **Account #** — bank-style routing/account numbers via Presidio's built-in recognizer

---

## Setup (one time)

You need **Python 3.10+** installed. Then, from this folder:

```bash
./setup.sh        # macOS / Linux / WSL
```

```powershell
.\setup.ps1       # Windows
```

This creates an isolated environment, installs the libraries, and downloads the
language model that recognizes names (~600 MB, so give it a few minutes).

> On Windows, if PowerShell blocks the script the first time, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## Run

```bash
./run.sh          # macOS / Linux / WSL
```

```powershell
.\run.ps1         # Windows
```

It starts the app and opens **http://localhost:8000** in your browser. To stop,
press `Ctrl+C` in the terminal.

## Use

1. Drag a document onto the page (or click to choose one).
2. Review the highlights. For every detection you can:
   - **Replace with fake** — generates realistic but invented data, seeded
     so the same entity gets the same fake everywhere
   - **Black out** — replaces with `[REDACTED]` (PDFs get *real* redaction —
     the underlying text is removed, not just covered by a box)
   - **Type tag** — replaces with a generic placeholder like `(name)`,
     `(ssn)`, `(address)`, `(medical id)`
   - **Keep** — leave the original value
3. Click **Download cleaned document**. A copy is also saved automatically to
   `backend/outputs/` with a timestamped filename, and shows up in the
   **Saved sessions** panel on the left for quick re-opening.

The review sidebar groups detections by type. Each group has a bulk picker —
flip every email to type-tag with one click, every name to fake, etc. — and
each individual cluster has the same four buttons for fine-grained control.
Click a group header to expand it and see the individual matches.

---

## How it works (for the curious)

```
upload → extract text → detect → cluster → you review → transform → download
                                                                      └→ also saved
                                                                         to outputs/
```

- **Detection** uses [Microsoft Presidio](https://github.com/microsoft/presidio)
  on top of a spaCy NER model. Names are found by the model from sentence
  *context*, not by patterns — and confidence is boosted when legal cue words
  ("plaintiff", "served on", "Atty.") sit nearby. Structured items (SSN, email,
  card, street addresses, case numbers, medical IDs) use custom recognizers
  layered on top of Presidio's built-ins. The threshold is set for **high
  recall**: it would rather over-flag and let you un-flag than miss something.
- **Clustering** groups mentions of the same entity so substitution is
  consistent — "James Lin" becomes the *same* fake name everywhere it appears,
  and a bare "Lin" attaches to the same person.
- **Fake data** is generated with [Faker](https://faker.readthedocs.io),
  seeded per entity so it's stable across runs and **format-preserving** (a fake
  SSN looks like an SSN, ICD-10 codes keep their letter-plus-decimal shape,
  fake dates stay plausible).
- **Medical IDs** rely on context: a 7-digit number near "MRN" or "patient"
  fires as a medical record, the same number floating in random text does not.
  Avoids over-flagging arbitrary ID numbers in non-medical documents.
- **PDF redaction is real** — it removes the underlying text via PyMuPDF's
  `apply_redactions`, not just a black box over still-extractable text.

## Privacy posture

- The **original** document (the one you uploaded) is never written to disk.
  It stays in memory only, behind a session ID, and is cleared on a 30-minute
  TTL sweep.
- The **cleaned** output (after your policies are applied) is written to
  `backend/outputs/<name>_scrubbed_<timestamp>.<ext>` on every Download. That
  file already has the PII removed — only the post-scrub version touches disk.
- Nothing is sent over the network. CORS is locked to `localhost`, and the app
  has no outbound HTTP at all once the model is loaded.

To purge saved outputs, delete the contents of `backend/outputs/`.

## Known limits

- **Scanned/image PDFs** aren't supported (no text to read). The app tells you
  if it gets one. OCR is a deliberate later addition.
- Name detection is very good but **not perfect** — which is exactly why the
  review step exists. On privileged material, always glance over the highlights
  before downloading.
- Clustering is exact-match + a last-name rule. It won't catch "the client"
  referring back to a named person (that's coreference, a later enhancement).
- The medical-ID recognizer requires a medical context word in the surrounding
  tokens (`MRN`, `patient`, `dx`, `icd`, `cpt`, `npi`, etc.). Bare numeric IDs
  in non-medical text are intentionally left alone.

## API (for scripting)

- `POST /analyze` (multipart file) — returns `{session_id, text, findings,
  clusters, report}`. The session lives in memory only.
- `POST /transform` (JSON: `{session_id, global_policy, overrides}`) — returns
  the cleaned file as an attachment. Also mirrors it to `backend/outputs/`.
  Response header `X-Scrub-Saved-Path` gives the absolute path of the saved
  copy.
- `GET /api/outputs` — list saved files (`[{name, size, mtime}, …]`).
- `GET /api/outputs/{name}` — fetch a saved file. Defaults to inline so
  browsers preview text/PDF; pass `?disposition=attachment` to force download.
- `GET /api/health` — `{status: "ok"}`.

Policy values for `global_policy` and entries in `overrides`:
`"fake"`, `"redact"`, `"label"`, `"keep"`.

## Project layout

```
backend/
  app/
    main.py              FastAPI: /analyze, /transform, /api/outputs
    core/detect.py       Presidio + legal/medical recognizers + tag map
    core/transform.py    clustering + seeded, format-preserving fake data
    adapters/            txt / docx / pdf  (extract + reassemble per format)
  outputs/               saved cleaned files (created on first download)
  requirements.txt
frontend/
  index.html             the whole UI (React via CDN, no build step)
setup.sh / setup.ps1     one-time install
run.sh   / run.ps1       launch the app
LICENSE                  MIT
```

## License

MIT. See [LICENSE](LICENSE).
