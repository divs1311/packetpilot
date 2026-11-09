# Evaluation

Everything here is reproducible:

```bash
python -m packetpilot.bench.generate --out benchmark/packages --n 48 --seed 7
python -m packetpilot.bench.evaluate --packages benchmark/packages --out benchmark/results
```

`benchmark/results/RESULTS.md` is the output of the last run; `scripts/check_benchmark.py` re-runs it in CI and
fails on regression.

## Dataset

48 fictional commercial submission packages across 12 industries (landscaping, HVAC, electrical, restaurant,
machine shop, janitorial, auto repair, bakery, IT consulting, printing, courier, plumbing), each with 3–8 documents:

| Document | Format | When included |
|---|---|---|
| Commercial application | PDF (text or scanned) | always |
| General liability supplement | PDF | GL requested |
| Loss run | PDF (text or scanned) | unless a defect removes it |
| Payroll schedule | XLSX | WC requested |
| Vehicle schedule | XLSX | Auto requested |
| Statement of values | XLSX | Property requested |
| Profit & loss statement | DOCX or PDF | ~45% of packages |
| Client email | EML | ~60% of packages |

| Slice | Packages | Purpose |
|---|---|---|
| Style A | 20 | `Label: value` forms, checkbox coverage lists, `$` amounts, US dates |
| Style B | 14 | two-column forms, different label wording, `USD` amounts, long-form dates, different table headers |
| Scanned | 8 | style A/B application (and half the loss runs) rasterised at 200 dpi with skew, blur and speckle → OCR |
| Style C (held-out) | 6 | label phrasing and loss-run headers chosen to be **outside** the rules extractor's dictionary |

Each package has `ground_truth.json`: the 24 field values as stated in the application, the schedule rows, the
injected defects and the issues they must raise.

**Injected defects (29 types, 63 instances).** Missing FEIN / effective date / revenue / payroll; missing loss run,
payroll schedule, vehicle schedule or statement of values; subcontractors "Yes" without explanation; revenue, FEIN,
headcount or effective-date conflicts between documents; claim marked open on the loss run but closed in an email;
duplicate claim rows; loss dated after the report; losses denied on the application but present on the loss run;
open claim with $0 reserve; loss run older than 90 days; payroll schedule not matching stated payroll; schedule
total row not adding up; headcount mismatch; vehicle count mismatch; VIN with a bad check digit; duplicate VIN;
statement-of-values location not on the application; invalid FEIN; effective date in the past; gap between prior
expiration and new effective date. 25% of packages are clean.

**Benign variations** test precision: legal name written `Acme, L.L.C.` on the supplement, emails restating the
correct revenue, headcount or effective date, and closed claims described as closed.

## Metrics

| Metric | Definition |
|---|---|
| Field accuracy | correct values / fields present in ground truth (fields with an injected conflict are excluded and scored by detection instead) |
| Silent errors | wrong or spurious values with status `auto_verified` — the case where a reviewer could bulk-approve a wrong value |
| Issue recall | injected issues raised with the right code (and field/subject where specified), by category |
| Issue precision | share of raised data-quality issues that match an injected defect |
| Evidence traceability | extracted values (fields and schedule rows) whose evidence text is found on the cited page of the cited document, re-checked against the parsed source |
| Clean pass | defect-free packages with no blocking data-quality issue |
| Defect blocked | defective packages where every injected issue was raised and readiness is blocked |
| Review touches | fields needing individual attention + open review-category issues per package |

## Results — deterministic extractor (`rules-v1`)

| Slice | Field accuracy | Silent errors | Missing recall | Contradiction recall | Validity recall | Issue precision | Traceability | Clean pass | Defect blocked |
|---|---|---|---|---|---|---|---|---|---|
| **Styles A + B + scanned** (42) | **99.9%** | **0** | 100% (17/17) | 100% (15/15) | 100% (23/23) | 98.2% | 100% | 90.0% | 100% |
| Style A (20) | 100% | 0 | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| Style B (14) | 100% | 0 | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| Scanned / OCR (8) | 99.4% | 0 | 100% | 100% | 100% | 87.5% | 100% | 66.7% | 100% |
| Style C, held-out (6) | 39.0% | **0** | 100% (1/1) | 50% (1/2) | 40% (2/5) | 10.0% | 100% | 0% | 0% |
| **All 48** | 92.5% | 0 | 100% | 94.1% | 89.3% | 61.5% | 100% | 75.0% | 88.9% |

Other measurements: claims rows 93% recalled (100% outside style C), vehicles and payroll rows 100%; median
processing time 0.03 s per digital package and about 3 s per scanned package (OCR); on average 16.9 fields per package were
eligible for bulk approval and 3.1 needed individual attention.

## How to read this

1. **In-distribution numbers are not a generalisation claim.** The same person wrote the generator and the rules
   extractor, so styles A and B mostly prove that parsing, reconciliation, rules and scoring are wired correctly.
   They're a regression gate, not evidence of real-world accuracy.
2. **The held-out slice is the honest one.** On unfamiliar phrasing the deterministic extractor read 39% of fields.
   What matters is *how* it failed: zero wrong values were auto-verified, every unread required field became a
   blocking `MISSING_REQUIRED_FIELD` issue, and all six packages were kept out of "ready". Its 36 false positives
   (30 `MISSING_REQUIRED_FIELD`, 6 `LOSS_RUN_UNDATED` because the held-out loss run says "Run Date") are the system
   correctly saying "I couldn't read this" — costly in reviewer time, not in wrong data.
3. **OCR** was 99.4% accurate after despeckling; the one miss (a `7` read as `T`) was flagged as unparseable, not
   guessed. OCR-only values are never eligible for bulk approval.
4. **The LLM path has not been benchmarked with a live model.** The adapters, schema, grounding and hybrid merge are
   covered by tests with a scripted client (including fabricated quotes and quotes that don't support the value),
   but no API key was available while building. Running it is one command:

   ```bash
   OPENAI_API_KEY=... python -m packetpilot.bench.evaluate --extractor hybrid --out benchmark/results-hybrid
   ```

   The style C slice is where hybrid mode should show its value; publish both tables side by side.
5. **Synthetic documents are cleaner than real ones.** Real loss runs vary by carrier, applications are partly
   handwritten, and emails are messier. The next evaluation step is a small labelled set built from public sample
   forms and volunteer brokers' redacted documents.

## Against the original targets

| Target | Status |
|---|---|
| ≥95% field-level accuracy on defined test fields | Met in-distribution (99.9%); **not met** on held-out phrasing with rules only (39%) — LLM run pending |
| ≥90% recall of deliberately introduced critical issues | Met in-distribution (100%); 50% on held-out style |
| 100% source traceability for accepted fields | Met: 100% of extracted values trace to a verbatim line on the cited page |
| Reduction in review time vs a measured manual baseline | **Not measured yet** — protocol below |

## Review-time study protocol (to run with real people)

The app already records `first_review_at`, every review action and `ready_at`, and the operations dashboard reports
median review minutes and correction rate.

1. Recruit 4–6 people who process submissions (account managers, CSRs, submission specialists).
2. Pick 12 packages: 4 clean, 8 with defects, mixed styles including scanned. Shuffle into two balanced sets.
3. **Manual arm:** participant keys the 24 fields into a spreadsheet template and lists every problem they'd send
   back to the client. Time from opening the first document to finishing the list.
4. **PacketPilot arm:** same participant, other set, using the reviewer UI until they mark ready or request info.
   Time comes from the audit log.
5. Counterbalance which arm goes first. Score both arms against `ground_truth.json` for accuracy and issue recall
   so speed isn't bought with errors.
6. Report median minutes per package, accuracy and issues caught per arm, with all individual times.
