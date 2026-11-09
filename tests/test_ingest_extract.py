from email.message import EmailMessage

from conftest import make_pdf, make_xlsx, needs_tesseract

from packetpilot.classify import classify
from packetpilot.extract import RulesExtractor
from packetpilot.ingest import parse_bytes
from packetpilot.schema import DocType, DocumentInfo


def _extract(filename: str, data: bytes, doc_type: DocType | None = None):
    parsed = parse_bytes(filename, data)
    cls = classify(parsed)
    info = DocumentInfo(id="d1", filename=filename, sha256="x", doc_type=(doc_type or cls.doc_type).value)
    return parsed, cls, RulesExtractor().extract(info, parsed)


def _values(res):
    return {c.path: c.value for c in res.candidates}


def test_two_column_form_and_blank_value_does_not_steal_next_label():
    pdf = make_pdf([
        (50, 60, "COMMERCIAL INSURANCE APPLICATION"),
        (50, 100, "Named Insured"), (250, 100, "Cedar Point Bakehouse LLC"),
        (50, 117, "Federal Employer Identification Number"),  # blank value
        (50, 134, "Form of Business"), (250, 134, "Limited Liability Company"),
        (50, 151, "Annual Gross Sales"), (250, 151, "USD 1,250,000"),
        (50, 168, "Requested Effective Date"), (250, 168, "March 1, 2027"),
        (50, 185, "Lines of Coverage Requested"), (250, 185, "General Liability, Workers Compensation"),
    ])
    parsed, cls, res = _extract("app.pdf", pdf)
    assert cls.doc_type == DocType.APPLICATION
    v = _values(res)
    assert v["applicant.legal_name"] == "Cedar Point Bakehouse LLC"
    assert "applicant.fein" not in v
    assert v["applicant.entity_type"] == "LLC"
    assert v["financials.annual_revenue"] == 1_250_000
    assert v["policy.effective_date"] == "2027-03-01"
    assert v["policy.lines_requested"] == ["GL", "WC"]
    cand = next(c for c in res.candidates if c.path == "financials.annual_revenue")
    assert cand.evidence.page == 1 and cand.evidence.bbox and "USD 1,250,000" in cand.evidence.text


def test_label_on_its_own_line_reads_next_line():
    pdf = make_pdf([(50, 60, "Commercial Insurance Application"), (50, 100, "Description of Operations:"),
                    (50, 113, "Retail bakery producing breads and pastries"), (50, 140, "FEIN: 12-3456789")])
    _, _, res = _extract("app.pdf", pdf, DocType.APPLICATION)
    v = _values(res)
    assert v["operations.description"] == "Retail bakery producing breads and pastries"
    assert v["applicant.fein"] == "12-3456789"


def test_loss_run_table_with_merged_header_cells():
    pdf = make_pdf([
        (50, 60, "Loss Run Report"), (50, 80, "Valuation Date: 10/01/2026"),
        (36, 120, "Claim #"), (118, 120, "Loss Date Coverage Cause"), (380, 120, "Total Paid"), (470, 120, "Status"),
        (36, 135, "GL-2024-10001"), (118, 135, "03/14/2024"), (178, 135, "GL"), (215, 135, "Slip and fall"),
        (380, 135, "$12,500.00"), (470, 135, "Closed"),
        (36, 150, "WC-2025-20002"), (118, 150, "06/02/2025"), (178, 150, "WC"), (215, 150, "Back strain"),
        (380, 150, "$4,000.00"), (470, 150, "Open"),
    ])
    _, cls, res = _extract("loss_run.pdf", pdf)
    assert cls.doc_type == DocType.LOSS_RUN
    assert res.meta["valuation_date"] == "2026-10-01"
    claims = {r.key: r.data for r in res.records}
    assert claims["GL-2024-10001"]["paid"] == 12_500 and claims["GL-2024-10001"]["status"] == "Closed"
    assert claims["WC-2025-20002"]["line"] == "WC" and claims["WC-2025-20002"]["description"] == "Back strain"


def test_payroll_schedule_rows_and_total():
    xlsx = make_xlsx([
        ["Payroll Schedule - Test Co"], [],
        ["State", "Class Code", "Description", "Employees", "Annual Payroll"],
        ["OH", "0042", "Landscape gardening", 9, 410000],
        ["OH", "8810", "Clerical", 2, 90000],
        ["Total", None, None, 11, 500000],
    ])
    _, cls, res = _extract("payroll.xlsx", xlsx)
    assert cls.doc_type == DocType.PAYROLL_SCHEDULE
    rows = {r.key: r.data for r in res.records}
    assert rows["OH-0042"]["payroll"] == 410_000 and rows["OH-8810"]["employees"] == 2
    assert res.meta["stated_total_payroll"] == 500_000 and res.meta["stated_total_employees"] == 11
    assert res.records[0].evidence.locator.startswith("Sheet!")


def test_sov_ignores_unrecognised_columns():
    xlsx = make_xlsx([
        ["Statement of Values"], ["Loc #", "Address", "City", "State", "ZIP", "Building Value", "Contents"],
        [1, "481 Cedar Ave", "Columbus", "OH", "43215", 900000, 120000],
    ])
    _, _, res = _extract("sov.xlsx", xlsx)
    assert res.records[0].data["address"] == "481 Cedar Ave"
    assert res.records[0].key == "481 cedar ave"


def test_email_statements_and_wrapped_claim_sentence():
    msg = EmailMessage()
    msg["From"] = "Maria <maria@x.example>"
    msg["Subject"] = "Submission"
    msg.set_content("Hello,\n\nOur revenue for last year came in at $2,100,000. We now have 22 full-time\n"
                    "employees. Also, claim GL-2024-10001 was closed by the carrier in June.\n"
                    "We need coverage effective March 1, 2027.\n")
    parsed, cls, res = _extract("note.eml", msg.as_bytes())
    assert cls.doc_type == DocType.EMAIL
    v = _values(res)
    assert v["financials.annual_revenue"] == 2_100_000
    assert v["workforce.full_time_employees"] == 22
    assert v["policy.effective_date"] == "2027-03-01"
    claim = res.records[0]
    assert claim.partial and claim.key == "GL-2024-10001" and claim.data["status"] == "Closed"


def test_email_attachments_are_exposed():
    msg = EmailMessage()
    msg["Subject"] = "docs"
    msg.set_content("see attached")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="loss_run.pdf")
    parsed = parse_bytes("m.eml", msg.as_bytes())
    assert [a.filename for a in parsed.attachments] == ["loss_run.pdf"]


def test_docx_financial_statement_only_total_revenue():
    import io

    import docx

    d = docx.Document()
    d.add_heading("Profit and Loss Statement", 1)
    d.add_paragraph("EIN: 12-3456789")
    t = d.add_table(rows=0, cols=2)
    for a, b in [("Revenue", ""), ("Service revenue", "$1,150,000"), ("Total Revenue", "$1,200,000"), ("Net Income", "$90,000")]:
        r = t.add_row().cells
        r[0].text, r[1].text = a, b
    buf = io.BytesIO()
    d.save(buf)
    _, cls, res = _extract("fin.docx", buf.getvalue())
    assert cls.doc_type == DocType.FINANCIAL_STATEMENT
    revs = [c.value for c in res.candidates if c.path == "financials.annual_revenue"]
    assert revs == [1_200_000]
    assert _values(res)["applicant.fein"] == "12-3456789"


def test_unknown_document_is_unclassified():
    _, cls, _ = _extract("notes.txt", b"lunch order: two sandwiches")
    assert cls.doc_type == DocType.OTHER


def test_corrupt_pdf_does_not_raise():
    parsed = parse_bytes("broken.pdf", b"%PDF-1.4 not really")
    assert parsed.warnings and not parsed.pages


@needs_tesseract
def test_ocr_fallback_on_image_only_pdf():
    import io

    import pymupdf
    from PIL import Image

    src = make_pdf([(50, 60, "Commercial Insurance Application"), (50, 100, "FEIN: 12-3456789"),
                    (50, 120, "Annual Revenue: $1,200,000")])
    with pymupdf.open(stream=src, filetype="pdf") as doc:
        pix = doc[0].get_pixmap(dpi=200)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=200)
    parsed, cls, res = _extract("scan.pdf", buf.getvalue())
    assert parsed.ocr_pages == [1]
    cand = {c.path: c for c in res.candidates}
    assert cand["applicant.fein"].value == "12-3456789"
    assert cand["financials.annual_revenue"].value == 1_200_000
    assert cand["applicant.fein"].evidence.method.startswith("ocr+")
