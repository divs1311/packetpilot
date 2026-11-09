"""Render fictional companies into submission documents (PDF, XLSX, DOCX, EML).

Layouts are original. They are *inspired by* the information commercial
applications collect (ACORD 125/126-style content) but do not reproduce any
ACORD form. Every document carries a "SYNTHETIC" footer.

Styles
  A  label: value lines, checkbox coverage list, US dates, $ amounts
  B  two-column table layout, alternative label wording, long dates, "USD" amounts
  C  held-out phrasing: labels deliberately *outside* the rules extractor's
     dictionary, simulating an unfamiliar agency/carrier form
"""

from __future__ import annotations

import io
import random
import textwrap
from datetime import date
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

from .company import Building, Claim, Company, PayrollRow, Vehicle

FOOTER = "SYNTHETIC DOCUMENT - fictional data generated for PacketPilot benchmark"
LINE_NAMES = {"GL": "General Liability", "AUTO": "Commercial Auto", "WC": "Workers Compensation", "PROPERTY": "Commercial Property"}
LOSS_LINE = {"GL": "GL", "AUTO": "Auto", "WC": "WC", "PROPERTY": "Property"}
ENTITY_B = {"LLC": "Limited Liability Company", "Corporation": "Corporation", "Partnership": "General Partnership",
            "Sole Proprietor": "Sole Proprietorship"}

LABELS = {
    "A": {
        "applicant.legal_name": "Applicant Name", "applicant.dba": "DBA", "applicant.fein": "FEIN",
        "applicant.entity_type": "Entity Type", "applicant.mailing_address": "Mailing Address",
        "applicant.years_in_business": "Years in Business", "applicant.naics": "NAICS Code",
        "contact.name": "Contact Name", "contact.email": "Contact Email", "contact.phone": "Contact Phone",
        "operations.description": "Description of Operations", "financials.annual_revenue": "Annual Revenue",
        "financials.total_payroll": "Total Annual Payroll", "workforce.full_time_employees": "Full-Time Employees",
        "workforce.part_time_employees": "Part-Time Employees", "policy.effective_date": "Proposed Effective Date",
        "policy.lines_requested": "Coverages Requested", "prior.carrier": "Prior Carrier",
        "prior.expiration_date": "Prior Policy Expiration", "loss_history.prior_losses_disclosed": "Any losses in the past 5 years?",
        "auto.vehicle_count": "Number of Vehicles", "location": "Location {i}",
    },
    "B": {
        "applicant.legal_name": "Named Insured", "applicant.dba": "D/B/A", "applicant.fein": "Federal Employer Identification Number",
        "applicant.entity_type": "Form of Business", "applicant.mailing_address": "Business Address",
        "applicant.years_in_business": "Years in Operation", "applicant.naics": "NAICS",
        "contact.name": "Primary Contact", "contact.email": "E-mail", "contact.phone": "Telephone",
        "operations.description": "Nature of Business", "financials.annual_revenue": "Annual Gross Sales",
        "financials.total_payroll": "Estimated Annual Payroll", "workforce.full_time_employees": "Number of Full-Time Employees",
        "workforce.part_time_employees": "Number of Part-Time Employees", "policy.effective_date": "Requested Effective Date",
        "policy.lines_requested": "Lines of Coverage Requested", "prior.carrier": "Current Carrier",
        "prior.expiration_date": "Current Policy Expiration", "loss_history.prior_losses_disclosed": "Losses in the last five years",
        "auto.vehicle_count": "# of Vehicles", "location": "Premises Address {i}",
    },
    "C": {
        "applicant.legal_name": "Name of Business Entity", "applicant.dba": "Trade Style", "applicant.fein": "Employer ID",
        "applicant.entity_type": "Organization Form", "applicant.mailing_address": "Correspondence Address",
        "applicant.years_in_business": "Years Operating", "applicant.naics": "Industry Code",
        "contact.name": "Point of Contact", "contact.email": "Email", "contact.phone": "Telephone",
        "operations.description": "Business Activities", "financials.annual_revenue": "Gross Annual Receipts",
        "financials.total_payroll": "Total Wages Paid", "workforce.full_time_employees": "Persons Employed (FT)",
        "workforce.part_time_employees": "Persons Employed (PT)", "policy.effective_date": "Coverage Start",
        "policy.lines_requested": "Products Needed", "prior.carrier": "Incumbent Insurer",
        "prior.expiration_date": "Current Term Ends", "loss_history.prior_losses_disclosed": "Loss Record (5 yrs)",
        "auto.vehicle_count": "Fleet Size", "location": "Site {i}",
    },
}


def fmt_money(v: float | None, style: str) -> str:
    if v is None:
        return ""
    if style == "B":
        return f"USD {v:,.0f}"
    return f"${v:,.0f}"


def fmt_date(d: date | None, style: str) -> str:
    if d is None:
        return ""
    if style == "B":
        return f"{d:%B} {d.day}, {d.year}"
    if style == "C":
        return d.isoformat()
    return f"{d:%m/%d/%Y}"


def fmt_lines(lines: list[str], style: str) -> str:
    if style == "A":
        return "   ".join(f"[{'X' if k in lines else ' '}] {LINE_NAMES[k]}" for k in ("GL", "AUTO", "WC", "PROPERTY"))
    return ", ".join(LINE_NAMES[k] for k in lines)


def fmt_bool(v: bool | None, style: str) -> str:
    if v is None:
        return ""
    if style == "A":
        return f"[{'X' if v else ' '}] Yes   [{' ' if v else 'X'}] No"
    return "Yes" if v else "No"


# --------------------------------------------------------------------------- #
# PDF primitives
# --------------------------------------------------------------------------- #


class PDF:
    def __init__(self):
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=letter, invariant=1)
        self.w, self.h = letter
        self.y = self.h - 60

    def title(self, text: str, sub: str | None = None):
        self.c.setFont("Helvetica-Bold", 14)
        self.c.drawString(50, self.y, text)
        self.y -= 18
        if sub:
            self.c.setFont("Helvetica", 9)
            self.c.drawString(50, self.y, sub)
            self.y -= 14
        self.y -= 8

    def section(self, text: str):
        self.need(40)
        self.y -= 6
        self.c.setFont("Helvetica-Bold", 10)
        self.c.drawString(50, self.y, text.upper())
        self.y -= 18

    def kv_inline(self, label: str, value: str):
        self.need(20)
        self.c.setFont("Helvetica", 9.5)
        self.c.drawString(50, self.y, f"{label}: {value}".rstrip() if value else f"{label}:")
        self.y -= 16

    def kv_columns(self, label: str, value: str):
        self.need(20)
        self.c.setFont("Helvetica", 9)
        self.c.drawString(50, self.y, label)
        if value:
            self.c.drawString(250, self.y, value)
        self.c.setLineWidth(0.3)
        self.c.line(48, self.y - 4, self.w - 48, self.y - 4)
        self.y -= 17

    def text(self, s: str, size: float = 9, x: float = 50):
        self.need(16)
        self.c.setFont("Helvetica", size)
        self.c.drawString(x, self.y, s)
        self.y -= 14

    def row(self, cells: list[tuple[float, str, str]], size: float = 8, bold: bool = False):
        """cells: (x, text, align 'l'|'r')"""
        self.need(16)
        self.c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        for x, txt, align in cells:
            if align == "r":
                self.c.drawRightString(x, self.y, txt)
            else:
                self.c.drawString(x, self.y, txt)
        self.y -= 14

    def need(self, space: float):
        if self.y < 60 + space:
            self.new_page()

    def new_page(self):
        self._footer()
        self.c.showPage()
        self.y = self.h - 60

    def _footer(self):
        self.c.setFont("Helvetica-Oblique", 7)
        self.c.drawString(50, 30, FOOTER)

    def bytes(self) -> bytes:
        self._footer()
        self.c.save()
        return self.buf.getvalue()


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


def application_pdf(co: Company, style: str, over: dict, *, vehicle_count: int | None) -> bytes:
    """``over`` maps field path -> replacement value; a value of None blanks the field."""
    L = LABELS[style]

    def v(path, default):
        return over[path] if path in over else default

    vals = {
        "applicant.legal_name": v("applicant.legal_name", co.legal_name),
        "applicant.dba": v("applicant.dba", co.dba) or "N/A",
        "applicant.fein": v("applicant.fein", co.fein) or "",
        "applicant.entity_type": ENTITY_B[co.entity_type] if style == "B" else co.entity_type,
        "applicant.mailing_address": co.mailing_address,
        "applicant.years_in_business": str(co.years_in_business),
        "applicant.naics": co.naics,
        "contact.name": co.contact_name,
        "contact.email": co.contact_email,
        "contact.phone": co.contact_phone,
        "operations.description": co.ops,
        "financials.annual_revenue": fmt_money(v("financials.annual_revenue", co.revenue), style),
        "financials.total_payroll": fmt_money(v("financials.total_payroll", co.payroll), style),
        "workforce.full_time_employees": str(v("workforce.full_time_employees", co.ft)),
        "workforce.part_time_employees": str(co.pt),
        "policy.effective_date": fmt_date(v("policy.effective_date", co.effective_date), style),
        "policy.lines_requested": fmt_lines(co.lines, style),
        "prior.carrier": co.prior_carrier,
        "prior.expiration_date": fmt_date(v("prior.expiration_date", co.prior_expiration), style),
        "loss_history.prior_losses_disclosed": fmt_bool(v("loss_history.prior_losses_disclosed", co.losses_disclosed), style),
    }
    pdf = PDF()
    title = {"A": "Commercial Insurance Application", "B": "COMMERCIAL INSURANCE APPLICATION",
             "C": "Commercial Insurance Application - Agency Intake Form"}[style]
    pdf.title(title, "Synthetic form for testing. Not an ACORD form.")
    put = pdf.kv_inline if style in ("A", "C") else pdf.kv_columns

    sections = [
        ("Applicant Information", ["applicant.legal_name", "applicant.dba", "applicant.fein", "applicant.entity_type",
                                   "applicant.mailing_address", "applicant.years_in_business", "applicant.naics"]),
        ("Contact", ["contact.name", "contact.email", "contact.phone"]),
        ("Operations", ["operations.description", "financials.annual_revenue", "financials.total_payroll",
                        "workforce.full_time_employees", "workforce.part_time_employees"]),
    ]
    for name, paths in sections:
        pdf.section(name)
        for p in paths:
            put(L[p], vals[p])
        if name == "Operations":
            for i, loc in enumerate(co.locations, 1):
                put(L["location"].format(i=i), loc)
    pdf.new_page()
    pdf.section("Coverage Requested")
    for p in ("policy.effective_date", "policy.lines_requested"):
        put(L[p], vals[p])
    if vehicle_count is not None:
        put(L["auto.vehicle_count"], str(vehicle_count))
    pdf.section("Prior Insurance and Loss History")
    for p in ("prior.carrier", "prior.expiration_date", "loss_history.prior_losses_disclosed"):
        put(L[p], vals[p])
    pdf.text("")
    pdf.text("I certify the information in this application is true and complete. (Synthetic signature)", 8)
    return pdf.bytes()


def supplement_pdf(co: Company, style: str, over: dict) -> bytes:
    def v(path, default):
        return over[path] if path in over else default

    labels = {
        "A": ["Applicant Name", "FEIN", "Description of Operations", "Annual Gross Sales", "Do you use subcontractors?",
              "If yes, explain", "Annual cost of subcontracted work"],
        "B": ["Named Insured", "FEIN #", "Nature of Business", "Gross Sales", "Any work subcontracted?",
              "If yes, describe", "Subcontractor costs"],
    }["B" if style == "B" else "A"]
    used = v("gl.subcontractors_used", co.subcontractors_used)
    values = [
        v("applicant.legal_name", co.legal_name),
        v("applicant.fein", co.fein) or "",
        co.ops,
        fmt_money(v("financials.annual_revenue", co.revenue), style),
        f"[{'X' if used else ' '}] Yes   [{' ' if used else 'X'}] No" if style != "B" else ("Yes" if used else "No"),
        v("gl.subcontractor_explanation", co.sub_explanation) or "",
        fmt_money(co.sub_cost, style) if used and co.sub_cost else "",
    ]
    pdf = PDF()
    pdf.title("General Liability Supplemental Application", "Synthetic supplement for testing. Not an ACORD form.")
    put = pdf.kv_columns if style == "B" else pdf.kv_inline
    pdf.section("Applicant")
    for lab, val in list(zip(labels, values))[:4]:
        put(lab, val)
    pdf.section("Subcontracted Work")
    for lab, val in list(zip(labels, values))[4:]:
        put(lab, val)
    pdf.section("Products and Completed Operations")
    put("Products sold or manufactured" if style != "B" else "Products", "None")
    return pdf.bytes()


def loss_run_pdf(co: Company, style: str, claims: list[Claim], valuation: date, carrier: str) -> bytes:
    pdf = PDF()
    pdf.title("Loss Run Report" if style != "C" else "Loss Run Report - Carrier Extract", "Synthetic report for testing.")
    pdf.text(f"Insured: {co.legal_name}")
    pdf.text(f"Carrier: {carrier}")
    start = date(valuation.year - 5, valuation.month, 1)
    pdf.text(f"Policy Period: {fmt_date(start, 'A')} - {fmt_date(valuation, 'A')}")
    pdf.text(f"Valuation Date: {fmt_date(valuation, 'A')}" if style != "C" else f"Run Date: {valuation.isoformat()}")
    pdf.text("")
    if style == "A":
        header = [(40, "Claim No.", "l"), (125, "Date of Loss", "l"), (190, "Line", "l"), (240, "Description", "l"),
                  (455, "Paid", "r"), (520, "Reserve", "r"), (535, "Status", "l")]
    elif style == "B":
        header = [(36, "Claim #", "l"), (108, "Loss Date", "l"), (162, "Coverage", "l"), (208, "Cause", "l"),
                  (412, "Total Paid", "r"), (470, "Outstanding", "r"), (528, "Incurred", "r"), (545, "Status", "l")]
    else:
        header = [(40, "Claim Ref", "l"), (125, "Occurrence Date", "l"), (205, "Kind", "l"), (252, "Narrative", "l"),
                  (470, "Indemnity Paid", "r"), (535, "Open Amount", "r"), (545, "Condition", "l")]
    pdf.row(header, bold=True, size=7.5)
    if not claims:
        pdf.text("No losses reported during this period.")
    for c in claims:
        if style == "B":
            vals = [c.claim_number, fmt_date(c.date_of_loss, "A"), LOSS_LINE[c.line], c.description,
                    f"${c.paid:,.2f}", f"${c.reserve:,.2f}", f"${c.paid + c.reserve:,.2f}", c.status]
        else:
            vals = [c.claim_number, fmt_date(c.date_of_loss, "A"), LOSS_LINE[c.line], c.description,
                    f"${c.paid:,.2f}", f"${c.reserve:,.2f}", c.status]
        pdf.row([(x, val, al) for (x, _, al), val in zip(header, vals)], size=7.5)
    if claims:
        paid = sum(c.paid for c in claims)
        res = sum(c.reserve for c in claims)
        tot = ["Totals", "", "", "", f"${paid:,.2f}", f"${res:,.2f}"] + ([f"${paid + res:,.2f}", ""] if style == "B" else [""])
        pdf.row([(x, val, al) for (x, _, al), val in zip(header, tot) if val], bold=True, size=7.5)
    return pdf.bytes()


def financials(co: Company, style: str, revenue: float, ein: str | None) -> tuple[str, bytes]:
    service = round(revenue * 0.96, -2)
    other = revenue - service
    cogs = round(revenue * 0.41, -2)
    opex = round(revenue * 0.12, -2)
    net = revenue - cogs - co.payroll - opex
    rows = [("Revenue", ""), ("Service and product revenue", f"${service:,.0f}"), ("Other income", f"${other:,.0f}"),
            ("Total Revenue", f"${revenue:,.0f}"), ("Cost of goods sold", f"${cogs:,.0f}"),
            ("Gross Profit", f"${revenue - cogs:,.0f}"), ("Salaries and wages", f"${co.payroll:,.0f}"),
            ("Other operating expenses", f"${opex:,.0f}"), ("Net Income", f"${net:,.0f}")]
    if style == "B":
        pdf = PDF()
        pdf.title("Profit and Loss Statement", "Synthetic financial statement for testing.")
        pdf.text(co.legal_name)
        if ein:
            pdf.text(f"EIN: {ein}")
        pdf.text(f"For the year ended December 31, {co.valuation_date.year - 1}")
        pdf.text("")
        for lab, val in rows:
            pdf.row([(50, lab, "l"), (400, val, "r")], size=9, bold=lab.startswith("Total") or lab == "Net Income")
        return "financial_statement.pdf", pdf.bytes()
    import docx

    d = docx.Document()
    d.add_heading("Profit and Loss Statement", level=1)
    d.add_paragraph(co.legal_name)
    if ein:
        d.add_paragraph(f"EIN: {ein}")
    d.add_paragraph(f"For the year ended December 31, {co.valuation_date.year - 1}")
    t = d.add_table(rows=0, cols=2)
    for lab, val in rows:
        cells = t.add_row().cells
        cells[0].text, cells[1].text = lab, val
    d.add_paragraph(FOOTER)
    buf = io.BytesIO()
    d.save(buf)
    return "financials_FY.docx", buf.getvalue()


def _xlsx(title: str, header: list[str], rows: list[list], total: list | None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Schedule"
    ws.append([title])
    ws.append([FOOTER])
    ws.append([])
    ws.append(header)
    for r in rows:
        ws.append(r)
    if total:
        ws.append(total)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def payroll_xlsx(co: Company, style: str, rows: list[PayrollRow], total: tuple[float, int] | None) -> bytes:
    pay_total, emp_total = total if total else (sum(r.payroll for r in rows), sum(r.employees for r in rows))
    if style == "B":
        header = ["WC Class", "Classification", "# of Employees", "Estimated Annual Remuneration", "State"]
        data = [[r.class_code, r.description, r.employees, r.payroll, r.state] for r in rows]
        tot = ["TOTAL", "", emp_total, pay_total, ""]
    else:
        header = ["State", "Class Code", "Description", "Employees", "Annual Payroll"]
        data = [[r.state, r.class_code, r.description, r.employees, r.payroll] for r in rows]
        tot = ["Total", "", "", emp_total, pay_total]
    return _xlsx(f"Payroll Schedule - {co.legal_name}", header, data, tot)


def vehicle_xlsx(co: Company, style: str, vehicles: list[Vehicle]) -> bytes:
    if style == "B":
        header = ["VIN", "Model Year", "Make", "Model", "Garage Zip Code", "Stated Value", "Use"]
        data = [[v.vin, v.year, v.make, v.model, v.garaging_zip, v.cost_new, "Service"] for v in vehicles]
    else:
        header = ["Year", "Make", "Model", "VIN", "Garaging ZIP", "Cost New"]
        data = [[v.year, v.make, v.model, v.vin, v.garaging_zip, v.cost_new] for v in vehicles]
    return _xlsx(f"Vehicle Schedule - {co.legal_name}", header, data, None)


def sov_xlsx(co: Company, buildings: list[Building]) -> bytes:
    header = ["Loc #", "Address", "City", "State", "ZIP", "Building Value", "Contents", "Year Built", "Construction"]
    data = [[i, b.address, b.city, b.state, b.zip, b.building_value, b.contents_value, b.year_built, b.construction]
            for i, b in enumerate(buildings, 1)]
    return _xlsx(f"Statement of Values - {co.legal_name}", header, data, None)


def email_eml(co: Company, statements: list[str], as_of: date) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"{co.contact_name} <{co.contact_email}>"
    msg["To"] = "New Business Team <submissions@brokerage.example>"
    msg["Subject"] = f"Insurance submission - {co.legal_name}"
    msg["Date"] = format_datetime(datetime(as_of.year, as_of.month, as_of.day, 9, 30, tzinfo=timezone.utc))
    first = co.contact_name.split()[0]
    paras = ["Hello,", "Please find attached our documents for the insurance renewal we discussed. A few notes:"]
    paras += statements
    paras += ["Let me know if you need anything else.", f"Thanks,\n{co.contact_name}\n{co.legal_name}\n{co.contact_phone}"]
    body = "\n\n".join(textwrap.fill(p, 76) if "\n" not in p else p for p in paras)
    msg.set_content(body)
    return msg.as_bytes()


def scan_pdf(pdf_bytes: bytes, rng: random.Random, dpi: int = 200) -> bytes:
    """Rasterise a PDF into an image-only 'scanned' PDF with mild skew and noise."""
    import pymupdf
    from PIL import Image, ImageFilter

    images = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), colorspace=pymupdf.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            img = img.rotate(rng.uniform(-0.6, 0.6), expand=False, fillcolor=255, resample=Image.BICUBIC)
            img = img.filter(ImageFilter.GaussianBlur(radius=0.4))
            px = img.load()
            for _ in range(img.width * img.height // 400):  # salt-and-pepper speckle
                x, y = rng.randrange(img.width), rng.randrange(img.height)
                px[x, y] = 0 if rng.random() < 0.5 else 255
            images.append(img.convert("RGB"))
    buf = io.BytesIO()
    images[0].save(buf, "PDF", resolution=dpi, save_all=True, append_images=images[1:])
    return buf.getvalue()
