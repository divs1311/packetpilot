# PacketPilot benchmark results

Generated 2026-11-09 · extractor `rules (rules-v1)` · 48 packages · generator seed 7

| Slice | Pkgs | Field accuracy | Silent errors | Missing recall | Contradiction recall | Validity recall | Issue precision | Traceability | Clean pass | Defect blocked |
|---|---|---|---|---|---|---|---|---|---|---|
| **Overall** | 48 | 92.5% | 0 | 100.0% | 94.1% | 89.3% | 61.5% | 100.0% | 75.0% | 88.9% |
| In-distribution (A, B, scanned) | 42 | 99.9% | 0 | 100.0% | 100.0% | 100.0% | 98.2% | 100.0% | 90.0% | 100.0% |
| scanned (OCR) | 8 | 99.4% | 0 | 100.0% | 100.0% | 100.0% | 87.5% | 100.0% | 66.7% | 100.0% |
| style A | 20 | 100.0% | 0 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| style B | 14 | 100.0% | 0 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| style C (held-out phrasing) | 6 | 39.0% | 0 | 100.0% | 50.0% | 40.0% | 10.0% | 100.0% | 0.0% | 0.0% |

## Detail

* Fields scored: 1014 (wrong 0, missed 76, spurious 0)
* Injected issues: 63 · false positives: 37
* Record recall: claims 93.0%, vehicles 100.0%, payroll 100.0%
* Median processing time per package: 0.03 s
* Mean fields needing individual review per package: 3.1 (mean bulk-approvable: 16.9)

### Recall by issue code

| Code | Detected |
|---|---|
| CLAIM_STATUS_CONFLICT | 1/1 |
| DUPLICATE_CLAIM | 5/6 |
| DUPLICATE_VIN | 2/2 |
| EFFECTIVE_DATE_PAST | 4/4 |
| EMPLOYEE_COUNT_MISMATCH | 4/4 |
| FIELD_CONFLICT | 6/6 |
| INCOMPLETE_EXPLANATION | 3/3 |
| INVALID_FEIN | 4/4 |
| INVALID_LOSS_DATE | 1/2 |
| INVALID_VIN | 2/2 |
| LOCATION_NOT_IN_APPLICATION | 3/3 |
| LOSS_DISCLOSURE_CONFLICT | 1/1 |
| MISSING_DOCUMENT | 7/7 |
| MISSING_REQUIRED_FIELD | 8/8 |
| PAYROLL_SCHEDULE_MISMATCH | 1/2 |
| PRIOR_COVERAGE_GAP | 2/3 |
| SCHEDULE_TOTAL_ARITHMETIC | 2/2 |
| STALE_LOSS_RUN | 3/3 |

### False positives by code

| Code | Count |
|---|---|
| MISSING_REQUIRED_FIELD | 31 |
| LOSS_RUN_UNDATED | 6 |

### Missed injected issues

* PKG-037: PRIOR_COVERAGE_GAP (prior.expiration_date)
* PKG-038: DUPLICATE_CLAIM (WC-2023-56838)
* PKG-039: INVALID_LOSS_DATE (WC-2026-14538)
* PKG-040: PAYROLL_SCHEDULE_MISMATCH (financials.total_payroll)

### Silent errors (wrong value marked auto-verified)

* none
