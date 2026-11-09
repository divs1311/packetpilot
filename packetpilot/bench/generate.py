"""Generate the synthetic benchmark: packages of documents + ground-truth JSON.

    python -m packetpilot.bench.generate --out benchmark/packages --n 48 --seed 7

Each package gets 0-3 deliberately injected defects (missing fields/documents,
cross-document contradictions, invalid values). The ground truth records the
true field values *as stated in the application* and the issues the injected
defects should raise. Benign variations (legal-name punctuation, consistent
e-mail restatements, closed claims mentioned as closed) are added so that
precision - not just recall - is tested.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .company import Claim, Company, make_company, make_vin
from .render import (
    LINE_NAMES,
    application_pdf,
    email_eml,
    financials,
    fmt_date,
    loss_run_pdf,
    payroll_xlsx,
    scan_pdf,
    sov_xlsx,
    supplement_pdf,
    vehicle_xlsx,
)

BENCH_AS_OF = date(2026, 11, 2)


@dataclass
class Pkg:
    co: Company
    style: str
    scanned: bool
    rng: random.Random
    as_of: date
    app_over: dict = field(default_factory=dict)
    sup_over: dict = field(default_factory=dict)
    include_loss_run: bool = True
    include_payroll: bool = True
    include_vehicles: bool = True
    include_sov: bool = True
    include_financials: bool = False
    include_email: bool = False
    fin_revenue: float | None = None
    fin_ein: str | None = None
    email_statements: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    valuation: date | None = None
    payroll_rows: list = field(default_factory=list)
    payroll_total: tuple | None = None
    vehicles: list = field(default_factory=list)
    app_vehicle_count: int | None = None
    buildings: list = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    expected: list[dict] = field(default_factory=list)
    conflict_fields: set[str] = field(default_factory=set)
    blank_fields: set[str] = field(default_factory=set)
    benign: list[str] = field(default_factory=list)
    revenue_in_email: bool = False
    effective_in_email: bool = False
    ft_in_email: bool = False

    def expect(self, code: str, field_path: str | None = None, subject: str | None = None):
        self.expected.append({"code": code, "field_path": field_path, "subject": subject})

    def has(self, line: str) -> bool:
        return line in self.co.lines


# --------------------------------------------------------------------------- #
# Defects
# --------------------------------------------------------------------------- #


def d_revenue_conflict(p: Pkg):
    other = round(p.co.revenue * p.rng.choice([1.75, 0.6, 1.4, 0.7]), -4)
    where = p.rng.choice(["financials", "email"] + (["supplement"] if p.has("GL") else []))
    if where == "financials":
        p.include_financials, p.fin_revenue = True, other
    elif where == "email":
        p.include_email = True
        p.email_statements.append(f"For reference, our revenue for last year came in at ${other:,.0f}.")
        p.revenue_in_email = True
    else:
        p.sup_over["financials.annual_revenue"] = other
    p.conflict_fields.add("financials.annual_revenue")
    p.expect("FIELD_CONFLICT", "financials.annual_revenue")


def d_fein_conflict(p: Pkg):
    digits = p.co.fein.replace("-", "")
    swapped = digits[:4] + digits[5] + digits[4] + digits[6:]
    if swapped == digits:
        swapped = digits[:-1] + str((int(digits[-1]) + 1) % 10)
    alt = f"{swapped[:2]}-{swapped[2:]}"
    if p.has("GL"):
        p.sup_over["applicant.fein"] = alt
    else:
        p.include_financials, p.fin_ein = True, alt
    p.conflict_fields.add("applicant.fein")
    p.expect("FIELD_CONFLICT", "applicant.fein")


def d_employee_conflict(p: Pkg):
    other = p.co.ft + p.rng.choice([4, 6, -3]) if p.co.ft > 4 else p.co.ft + 5
    p.include_email = True
    p.email_statements.append(f"We have grown a bit this year and now have {other} full-time employees.")
    p.conflict_fields.add("workforce.full_time_employees")
    p.expect("FIELD_CONFLICT", "workforce.full_time_employees")
    p.ft_in_email = True


def d_effective_conflict(p: Pkg):
    other = p.co.effective_date + timedelta(days=p.rng.choice([14, 31, -10]))
    p.include_email = True
    p.email_statements.append(f"We would like the new policies to be effective {other:%B} {other.day}, {other.year}.")
    p.conflict_fields.add("policy.effective_date")
    p.expect("FIELD_CONFLICT", "policy.effective_date")
    p.effective_in_email = True


def _open_claim(p: Pkg) -> Claim:
    opens = [c for c in p.claims if c.status == "Open"]
    if opens:
        return opens[0]
    if p.claims:
        c = p.claims[-1]
    else:
        ln = p.rng.choice(p.co.lines)
        dol = p.valuation - timedelta(days=p.rng.randint(60, 600))
        prefix = {"GL": "GL", "AUTO": "AU", "WC": "WC", "PROPERTY": "PR"}[ln]
        c = Claim(f"{prefix}-{dol.year}-{p.rng.randint(10000, 99999)}", dol, ln, "Customer slip and fall at premises", 1500.0, 0.0, "Closed")
        p.claims.append(c)
    c.status, c.reserve = "Open", float(p.rng.randint(20, 300) * 100)
    return c


def d_claim_status_conflict(p: Pkg):
    c = _open_claim(p)
    p.include_email = True
    p.email_statements.append(f"Also, claim {c.claim_number} was closed by the carrier back in the summer with no further payments.")
    p.expect("CLAIM_STATUS_CONFLICT", subject=c.claim_number)


def d_duplicate_claim(p: Pkg):
    if not p.claims:
        _open_claim(p)
        p.claims[-1].status, p.claims[-1].reserve = "Closed", 0.0
    c = p.rng.choice(p.claims)
    p.claims.append(copy.deepcopy(c))
    p.expect("DUPLICATE_CLAIM", subject=c.claim_number)


def d_future_loss(p: Pkg):
    if not p.claims:
        _open_claim(p)
    c = p.claims[-1]
    c.date_of_loss = p.valuation + timedelta(days=p.rng.randint(5, 30))
    p.expect("INVALID_LOSS_DATE", subject=c.claim_number)


def d_undisclosed_losses(p: Pkg):
    if not p.claims:
        _open_claim(p)
    p.app_over["loss_history.prior_losses_disclosed"] = False
    p.expect("LOSS_DISCLOSURE_CONFLICT", "loss_history.prior_losses_disclosed")


def d_open_no_reserve(p: Pkg):
    c = _open_claim(p)
    c.reserve = 0.0
    p.expect("CLAIM_RESERVE_INCONSISTENT", subject=c.claim_number)


def d_stale_loss_run(p: Pkg):
    shift = timedelta(days=p.rng.randint(150, 300))
    p.valuation = p.as_of - shift
    for c in p.claims:
        if c.date_of_loss >= p.valuation:
            c.date_of_loss = p.valuation - timedelta(days=p.rng.randint(20, 200))
    p.expect("STALE_LOSS_RUN")


def d_missing_loss_run(p: Pkg):
    p.include_loss_run = False
    p.expect("MISSING_DOCUMENT", subject="loss_run")


def d_missing_payroll_schedule(p: Pkg):
    p.include_payroll = False
    p.expect("MISSING_DOCUMENT", subject="payroll_schedule")


def d_missing_vehicle_schedule(p: Pkg):
    p.include_vehicles = False
    p.expect("MISSING_DOCUMENT", subject="vehicle_schedule")


def d_missing_sov(p: Pkg):
    p.include_sov = False
    p.expect("MISSING_DOCUMENT", subject="property_sov")


def d_payroll_mismatch(p: Pkg):
    factor = p.rng.choice([1.12, 1.2, 0.85, 0.9])
    for r in p.payroll_rows:
        r.payroll = round(r.payroll * factor, -2)
    p.payroll_total = None  # total row stays consistent with the rows
    p.expect("PAYROLL_SCHEDULE_MISMATCH", "financials.total_payroll")


def d_payroll_arithmetic(p: Pkg):
    pay = sum(r.payroll for r in p.payroll_rows)
    emp = sum(r.employees for r in p.payroll_rows)
    p.payroll_total = (pay + p.rng.choice([10_000, 25_000, -15_000]), emp)
    p.expect("SCHEDULE_TOTAL_ARITHMETIC")


def d_employee_mismatch(p: Pkg):
    p.payroll_rows[0].employees += p.rng.choice([2, 3, 5])
    p.expect("EMPLOYEE_COUNT_MISMATCH", "workforce.full_time_employees")


def d_vehicle_count(p: Pkg):
    p.app_vehicle_count = len(p.vehicles) + p.rng.choice([1, 2, -1])
    p.expect("VEHICLE_COUNT_MISMATCH", "auto.vehicle_count")


def d_invalid_vin(p: Pkg):
    v = p.rng.choice(p.vehicles)
    good = v.vin
    chk = good[8]
    alt = next(ch for ch in "0123456789X" if ch != chk)
    v.vin = good[:8] + alt + good[9:]
    p.expect("INVALID_VIN", subject=v.vin)


def d_duplicate_vin(p: Pkg):
    if len(p.vehicles) < 2:
        p.vehicles.append(copy.deepcopy(p.vehicles[0]))
        p.vehicles[-1].vin = make_vin(p.rng)
        p.app_vehicle_count = len(p.vehicles)
    p.vehicles[1].vin = p.vehicles[0].vin
    p.expect("DUPLICATE_VIN", subject=p.vehicles[0].vin)


def d_missing_explanation(p: Pkg):
    p.sup_over["gl.subcontractors_used"] = True
    p.sup_over["gl.subcontractor_explanation"] = None
    if not p.co.sub_cost:
        p.co.sub_cost = round(p.co.revenue * 0.05, -3)
    p.expect("INCOMPLETE_EXPLANATION", "gl.subcontractor_explanation")


def d_missing_fein(p: Pkg):
    p.app_over["applicant.fein"] = None
    p.sup_over["applicant.fein"] = None
    p.blank_fields.add("applicant.fein")
    p.expect("MISSING_REQUIRED_FIELD", "applicant.fein")


def d_missing_effective(p: Pkg):
    p.app_over["policy.effective_date"] = None
    p.blank_fields.add("policy.effective_date")
    p.expect("MISSING_REQUIRED_FIELD", "policy.effective_date")


def d_missing_revenue(p: Pkg):
    p.include_financials = False
    p.app_over["financials.annual_revenue"] = None
    p.sup_over["financials.annual_revenue"] = None
    p.blank_fields.add("financials.annual_revenue")
    p.expect("MISSING_REQUIRED_FIELD", "financials.annual_revenue")


def d_missing_payroll(p: Pkg):
    p.app_over["financials.total_payroll"] = None
    p.blank_fields.add("financials.total_payroll")
    p.expect("MISSING_REQUIRED_FIELD", "financials.total_payroll")


def d_effective_past(p: Pkg):
    eff = p.as_of - timedelta(days=p.rng.randint(5, 25))
    p.co.effective_date = eff
    p.co.prior_expiration = eff
    p.expect("EFFECTIVE_DATE_PAST", "policy.effective_date")


def d_coverage_gap(p: Pkg):
    p.co.prior_expiration = p.co.effective_date - timedelta(days=p.rng.randint(10, 45))
    p.expect("PRIOR_COVERAGE_GAP", "prior.expiration_date")


def d_invalid_fein(p: Pkg):
    bad = p.co.fein.replace("-", "")[:8]
    bad = f"{bad[:2]}-{bad[2:]}"
    p.co.fein = bad
    p.expect("INVALID_FEIN", "applicant.fein")


def d_sov_location(p: Pkg):
    b = copy.deepcopy(p.buildings[0])
    b.address = f"{p.rng.randint(100, 9800)} Riverside Ter"
    p.buildings.append(b)
    p.expect("LOCATION_NOT_IN_APPLICATION")


# name -> (fn, applicability, mutually exclusive tags)
DEFECTS = {
    "revenue_conflict": (d_revenue_conflict, lambda p: True, {"revenue"}),
    "fein_conflict": (d_fein_conflict, lambda p: True, {"fein", "fin"}),
    "employee_conflict": (d_employee_conflict, lambda p: True, {"ft"}),
    "effective_conflict": (d_effective_conflict, lambda p: True, {"eff"}),
    "claim_status_conflict": (d_claim_status_conflict, lambda p: True, {"lossrun", "claims"}),
    "duplicate_claim": (d_duplicate_claim, lambda p: True, {"lossrun", "claims"}),
    "future_loss_date": (d_future_loss, lambda p: True, {"lossrun", "claims", "valuation"}),
    "undisclosed_losses": (d_undisclosed_losses, lambda p: True, {"lossrun", "disclosure"}),
    "open_claim_no_reserve": (d_open_no_reserve, lambda p: True, {"lossrun", "claims"}),
    "stale_loss_run": (d_stale_loss_run, lambda p: True, {"lossrun", "valuation"}),
    "missing_loss_run": (d_missing_loss_run, lambda p: True, {"lossrun", "claims", "valuation", "disclosure"}),
    "missing_payroll_schedule": (d_missing_payroll_schedule, lambda p: p.has("WC"), {"payroll"}),
    "payroll_mismatch": (d_payroll_mismatch, lambda p: p.has("WC"), {"payroll", "paystated"}),
    "payroll_arithmetic": (d_payroll_arithmetic, lambda p: p.has("WC"), {"payroll"}),
    "employee_mismatch": (d_employee_mismatch, lambda p: p.has("WC"), {"payroll", "ft"}),
    "missing_vehicle_schedule": (d_missing_vehicle_schedule, lambda p: p.has("AUTO"), {"vehicles"}),
    "vehicle_count_mismatch": (d_vehicle_count, lambda p: p.has("AUTO"), {"vehicles"}),
    "invalid_vin": (d_invalid_vin, lambda p: p.has("AUTO"), {"vehicles"}),
    "duplicate_vin": (d_duplicate_vin, lambda p: p.has("AUTO"), {"vehicles"}),
    "missing_sov": (d_missing_sov, lambda p: p.has("PROPERTY"), {"sov"}),
    "sov_location_mismatch": (d_sov_location, lambda p: p.has("PROPERTY"), {"sov"}),
    "missing_explanation": (d_missing_explanation, lambda p: p.has("GL"), {"subs"}),
    "missing_fein": (d_missing_fein, lambda p: True, {"fein"}),
    "invalid_fein": (d_invalid_fein, lambda p: True, {"fein"}),
    "missing_effective_date": (d_missing_effective, lambda p: True, {"eff", "dates"}),
    "missing_revenue": (d_missing_revenue, lambda p: True, {"revenue", "fin"}),
    "missing_payroll": (d_missing_payroll, lambda p: p.has("WC"), {"paystated"}),
    "effective_date_past": (d_effective_past, lambda p: True, {"eff", "dates"}),
    "coverage_gap": (d_coverage_gap, lambda p: True, {"dates"}),
}


# --------------------------------------------------------------------------- #
# Package assembly
# --------------------------------------------------------------------------- #


def build_package(idx: int, style: str, scanned: bool, rng: random.Random, used: set[str], forced: list[str] | None = None) -> Pkg:
    as_of = BENCH_AS_OF - timedelta(days=rng.randint(0, 20))
    co = make_company(rng, as_of, used)
    p = Pkg(co=co, style=style, scanned=scanned, rng=rng, as_of=as_of)
    p.claims = copy.deepcopy(co.claims)
    p.valuation = co.valuation_date
    p.payroll_rows = copy.deepcopy(co.payroll_rows)
    p.vehicles = copy.deepcopy(co.vehicles)
    p.app_vehicle_count = len(co.vehicles) if p.has("AUTO") else None
    p.buildings = copy.deepcopy(co.buildings)
    p.include_financials = rng.random() < 0.45
    p.include_email = rng.random() < 0.6

    # choose defects
    if forced is not None:
        chosen = forced
    else:
        n = rng.choices([0, 1, 2, 3], weights=[25, 35, 25, 15])[0]
        names = [k for k, (_, ok, _) in DEFECTS.items() if ok(p)]
        rng.shuffle(names)
        chosen, tags = [], set()
        for name in names:
            if len(chosen) >= n:
                break
            t = DEFECTS[name][2]
            if t & tags:
                continue
            chosen.append(name)
            tags |= t
    for name in chosen:
        DEFECTS[name][0](p)
        p.defects.append(name)

    # the application's loss question reflects the loss run unless a defect says otherwise
    p.app_over.setdefault("loss_history.prior_losses_disclosed", bool(p.claims))

    # benign, consistent restatements (precision tests)
    if p.include_email:
        options = []
        if "financials.annual_revenue" not in p.conflict_fields | p.blank_fields:
            options.append(("rev", f"Our annual revenue is roughly ${co.revenue:,.0f}."))
        if "policy.effective_date" not in p.conflict_fields | p.blank_fields and "effective_date_past" not in p.defects:
            options.append(("eff", f"We need coverage effective {co.effective_date:%B} {co.effective_date.day}, {co.effective_date.year}."))
        if "workforce.full_time_employees" not in p.conflict_fields:
            options.append(("ft", f"We currently have {co.ft} full-time employees."))
        closed = [c for c in p.claims if c.status == "Closed"]
        if closed and p.include_loss_run:
            options.append(("claim", f"Claim {closed[0].claim_number} ({closed[0].description.lower()}) was settled and is closed."))
        rng.shuffle(options)
        for key, text in options[: rng.randint(1, max(1, len(options)))]:
            p.email_statements.append(text)
            p.benign.append(f"email_{key}")
    if p.has("GL") and "applicant.legal_name" not in p.sup_over and rng.random() < 0.3 and co.entity_type in ("LLC", "Corporation"):
        p.sup_over["applicant.legal_name"] = co.legal_name.replace(" LLC", ", L.L.C.").replace(" Inc.", ", Incorporated")
        p.benign.append("supplement_name_variant")
    if p.include_financials and p.fin_revenue is None:
        p.fin_revenue = co.revenue
    if p.include_financials and p.fin_ein is None and "fein" not in {t for d in p.defects for t in DEFECTS[d][2]}:
        p.fin_ein = co.fein if rng.random() < 0.5 else None
    return p


def render_package(p: Pkg, out_dir: Path) -> list[str]:
    co, style = p.co, p.style
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {}
    slug = "".join(ch for ch in co.legal_name.lower() if ch.isalnum())[:20]
    app_name = {"A": "commercial_application.pdf", "B": f"{slug}_application.pdf", "C": f"intake_form_{slug}.pdf"}[style]
    app = application_pdf(co, style, p.app_over, vehicle_count=p.app_vehicle_count)
    files[app_name] = scan_pdf(app, p.rng) if p.scanned else app
    if p.has("GL"):
        files["gl_supplement.pdf"] = supplement_pdf(co, style, p.sup_over)
    if p.include_loss_run:
        lr = loss_run_pdf(co, style, p.claims, p.valuation, co.prior_carrier)
        files[f"loss_run_{slug}.pdf"] = scan_pdf(lr, p.rng) if p.scanned and p.rng.random() < 0.5 else lr
    if p.has("WC") and p.include_payroll:
        files["payroll_schedule.xlsx"] = payroll_xlsx(co, p.rng.choice("AB"), p.payroll_rows, p.payroll_total)
    if p.has("AUTO") and p.include_vehicles:
        files["vehicle_schedule.xlsx"] = vehicle_xlsx(co, p.rng.choice("AB"), p.vehicles)
    if p.has("PROPERTY") and p.include_sov:
        files["statement_of_values.xlsx"] = sov_xlsx(co, p.buildings)
    if p.include_financials:
        name, data = financials(co, "B" if style == "B" else "A", p.fin_revenue, p.fin_ein)
        files[name] = data
    if p.include_email and p.email_statements:
        files["client_email.eml"] = email_eml(co, p.email_statements, p.as_of)
    for name, data in files.items():
        (out_dir / name).write_bytes(data)
    return sorted(files)


def ground_truth(p: Pkg, files: list[str], package_id: str) -> dict:
    co = p.co

    def val(path, default):
        if path in p.blank_fields:
            return None
        return p.app_over.get(path, default) if path in p.app_over else default

    gl = p.has("GL")
    used = p.sup_over.get("gl.subcontractors_used", co.subcontractors_used)
    expl = p.sup_over.get("gl.subcontractor_explanation", co.sub_explanation) if "gl.subcontractor_explanation" in p.sup_over else co.sub_explanation
    fields = {
        "applicant.legal_name": co.legal_name,
        "applicant.dba": co.dba,
        "applicant.fein": val("applicant.fein", co.fein),
        "applicant.entity_type": co.entity_type,
        "applicant.mailing_address": co.mailing_address,
        "applicant.years_in_business": co.years_in_business,
        "applicant.naics": co.naics,
        "contact.name": co.contact_name,
        "contact.email": co.contact_email,
        "contact.phone": co.contact_phone,
        "operations.description": co.ops,
        "financials.annual_revenue": val("financials.annual_revenue", co.revenue),
        "financials.total_payroll": val("financials.total_payroll", co.payroll),
        "workforce.full_time_employees": co.ft,
        "workforce.part_time_employees": co.pt,
        "policy.effective_date": (lambda d: d.isoformat() if d else None)(val("policy.effective_date", co.effective_date)),
        "policy.lines_requested": sorted(co.lines),
        "prior.carrier": co.prior_carrier,
        "prior.expiration_date": co.prior_expiration.isoformat(),
        "loss_history.prior_losses_disclosed": val("loss_history.prior_losses_disclosed", co.losses_disclosed),
        "auto.vehicle_count": p.app_vehicle_count if p.has("AUTO") else None,
        "gl.subcontractors_used": used if gl else None,
        "gl.subcontractor_explanation": (expl if used else None) if gl else None,
        "gl.subcontracted_cost": (co.sub_cost if used and co.sub_cost else None) if gl else None,
    }
    records = {
        "claims": [
            {"claim_number": c.claim_number, "date_of_loss": c.date_of_loss.isoformat(), "paid": c.paid,
             "reserve": c.reserve, "status": c.status}
            for c in p.claims
        ] if p.include_loss_run else [],
        "vehicles": [{"vin": v.vin, "year": v.year, "make": v.make} for v in p.vehicles] if p.has("AUTO") and p.include_vehicles else [],
        "payroll": [{"class_code": r.class_code, "employees": r.employees, "payroll": r.payroll} for r in p.payroll_rows]
        if p.has("WC") and p.include_payroll else [],
    }
    return {
        "package_id": package_id,
        "style": p.style,
        "scanned": p.scanned,
        "as_of": p.as_of.isoformat(),
        "lines": sorted(co.lines),
        "industry": co.industry,
        "files": files,
        "defects": p.defects,
        "benign_variations": p.benign,
        "expected_issues": p.expected,
        "conflict_fields": sorted(p.conflict_fields),
        "fields": fields,
        "records": records,
    }


PLAN = [("A", False)] * 20 + [("B", False)] * 14 + [("C", False)] * 6 + [("A", True)] * 4 + [("B", True)] * 4


def generate(out: Path, n: int = 48, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    manifest = []
    plan = (PLAN * (n // len(PLAN) + 1))[:n]
    for i, (style, scanned) in enumerate(plan, 1):
        pid = f"PKG-{i:03d}"
        p = build_package(i, style, scanned, random.Random(rng.random()), used)
        files = render_package(p, out / pid)
        gt = ground_truth(p, files, pid)
        (out / pid / "ground_truth.json").write_text(json.dumps(gt, indent=2))
        manifest.append({k: gt[k] for k in ("package_id", "style", "scanned", "industry", "lines", "defects", "files")})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (out / "generator.json").write_text(json.dumps({"seed": seed, "n": n, "reference_date": BENCH_AS_OF.isoformat(),
                                                    "plan": "20 A, 14 B, 6 C (held-out), 4 A scanned, 4 B scanned"}, indent=2))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="benchmark/packages")
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    m = generate(Path(a.out), a.n, a.seed)
    n_def = sum(len(x["defects"]) for x in m)
    print(f"Generated {len(m)} packages with {n_def} injected defects in {a.out}")


if __name__ == "__main__":
    main()
