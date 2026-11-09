"""LLM structured extraction (OpenAI or Gemini).

The model only *proposes* values. Each proposal must cite a page and a verbatim
quote; values are normalised with the same code the rules extractor uses and
then grounded against the parsed source (see ``grounding.py``). The model never
decides readiness, never computes totals and never resolves conflicts.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from ..config import Settings
from ..ingest import ParsedDocument
from ..normalize import clean_text, normalize_field, parse_date, parse_int, parse_lines, parse_money
from ..schema import DOC_TYPE_LABELS, FIELD_SPECS, Candidate, DocType, DocumentInfo, RecordState
from .base import ExtractionResult, evidence_from_lines, stable_id
from .grounding import best_line, ground_candidate
from .rules import _claim_key, _claim_status, _location_record, address_key

log = logging.getLogger(__name__)

MAX_CHARS = 60_000
LLM_BASE_CONFIDENCE = 0.9

SYSTEM_PROMPT = """You extract facts from commercial insurance submission documents for an insurance broker.
Rules:
- Extract only values that are explicitly written in the document. Never infer, estimate, total or convert.
- For every value, give the page number and a `quote`: the exact line of text from the document that contains it, copied verbatim.
- If a field is not present, leave it out. An empty answer is better than a guess.
- Values are strings exactly as written (e.g. "$1,200,000", "03/01/2027").
"""


class JsonLLM(Protocol):
    model_version: str

    def complete_json(self, system: str, user: str, schema: dict) -> dict: ...


def _nullable(t: str = "string") -> dict:
    return {"type": [t, "null"]}


def _obj(props: dict[str, dict]) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def response_schema() -> dict:
    field_item = _obj(
        {
            "path": {"type": "string", "enum": [*FIELD_SPECS.keys(), "loss_run.valuation_date"]},
            "value": {"type": "string"},
            "page": {"type": "integer"},
            "quote": {"type": "string"},
        }
    )
    ev = {"page": {"type": "integer"}, "quote": {"type": "string"}}
    claim = _obj({**{k: _nullable() for k in ("claim_number", "date_of_loss", "line", "description", "paid", "reserve", "status")}, **ev})
    vehicle = _obj({**{k: _nullable() for k in ("year", "make", "model", "vin", "garaging_zip", "cost_new")}, **ev})
    payroll = _obj({**{k: _nullable() for k in ("state", "class_code", "description", "employees", "payroll")}, **ev})
    location = _obj({"address": {"type": "string"}, **ev})
    prop = _obj({**{k: _nullable() for k in ("address", "city", "state", "zip", "building_value", "contents_value")}, **ev})
    return _obj(
        {
            "fields": {"type": "array", "items": field_item},
            "claims": {"type": "array", "items": claim},
            "vehicles": {"type": "array", "items": vehicle},
            "payroll": {"type": "array", "items": payroll},
            "locations": {"type": "array", "items": location},
            "property": {"type": "array", "items": prop},
        }
    )


def build_prompt(info: DocumentInfo, parsed: ParsedDocument) -> str:
    catalogue = "\n".join(f"- {s.path}: {s.label} ({s.kind})" for s in FIELD_SPECS.values())
    pages, used = [], 0
    for p in parsed.pages:
        chunk = f"=== Page {p.number} ===\n{p.text}\n"
        if used + len(chunk) > MAX_CHARS:
            pages.append(f"=== Page {p.number} === [truncated]\n")
            break
        pages.append(chunk)
        used += len(chunk)
    label = DOC_TYPE_LABELS.get(DocType(info.doc_type), info.doc_type)
    return (
        f"Document: {info.filename}\nDocument type: {label}\n\n"
        f"Field catalogue (use these paths):\n{catalogue}\n"
        "- loss_run.valuation_date: loss run valuation / as-of date (loss runs only)\n\n"
        "Also return table rows: claims (loss runs, or claim status statements in emails), vehicles, "
        "payroll rows (class code schedules), locations (premises addresses), property (statement of values).\n\n"
        f"Document text:\n{''.join(pages)}"
    )


class LLMExtractor:
    name = "llm"

    def __init__(self, client: JsonLLM):
        self.client = client
        self.version = client.model_version

    def extract(self, info: DocumentInfo, parsed: ParsedDocument) -> ExtractionResult:
        out = ExtractionResult()
        if not parsed.pages or not parsed.text.strip():
            return out
        try:
            data = self.client.complete_json(SYSTEM_PROMPT, build_prompt(info, parsed), response_schema())
        except Exception as exc:
            log.warning("LLM extraction failed for %s: %s", info.filename, exc)
            out.notes.append(f"LLM extraction failed for {info.filename}: {exc.__class__.__name__}")
            return out
        method = f"llm:{self.version}"

        for item in data.get("fields") or []:
            path, raw = item.get("path"), clean_text(item.get("value"))
            quote = item.get("quote") or ""
            page = _page(item, parsed)
            if not raw:
                continue
            if path == "loss_run.valuation_date":
                d = parse_date(raw)
                hit = best_line(parsed, page, quote)
                if d and hit:
                    out.meta.setdefault("valuation_date", d.isoformat())
                    out.meta_evidence.setdefault(
                        "valuation_date", evidence_from_lines(info, hit[0], hit[1], method, LLM_BASE_CONFIDENCE)
                    )
                continue
            if path not in FIELD_SPECS:
                continue
            value = normalize_field(path, raw)
            ev = evidence_from_lines(info, page, [], method, LLM_BASE_CONFIDENCE).model_copy(update={"text": quote})
            cand = Candidate(id=stable_id(info.id, path, method, raw, quote), path=path, value=value, raw=raw, evidence=ev)
            out.candidates.append(ground_candidate(cand, parsed))

        for coll, builder in (
            ("claims", _claim), ("vehicles", _vehicle), ("payroll", _payroll), ("locations", None), ("property", _property),
        ):
            for row in data.get(coll) or []:
                quote = row.get("quote") or ""
                hit = best_line(parsed, _page(row, parsed), quote)
                if hit is None:
                    out.notes.append(f"Dropped ungrounded {coll} row from LLM in {info.filename}")
                    continue
                ev = evidence_from_lines(info, hit[0], hit[1], method, LLM_BASE_CONFIDENCE, parsed.page(hit[0]).ocr)
                if coll == "locations":
                    rec = _location_record(info, row.get("address") or "", ev)
                    if rec:
                        out.records.append(rec)
                    continue
                built = builder(row)
                if built is None:
                    continue
                key, rec_data, partial = built
                out.records.append(
                    RecordState(
                        id=stable_id(info.id, coll, key, method, ev.locator), collection=coll, key=key,
                        data=rec_data, evidence=ev, partial=partial,
                    )
                )
        return out


def _page(item: dict, parsed: ParsedDocument) -> int:
    try:
        p = int(item.get("page") or 1)
    except (TypeError, ValueError):
        p = 1
    return p if parsed.page(p) else 1


def _money(v):
    return parse_money(v) if v else None


def _claim(row: dict):
    num = row.get("claim_number")
    if not num:
        return None
    key = _claim_key(num)
    dol = parse_date(row.get("date_of_loss"))
    lines = parse_lines(row.get("line") or "")
    data = {
        "claim_number": key,
        "date_of_loss": dol.isoformat() if dol else None,
        "line": lines[0] if lines else clean_text(row.get("line")),
        "description": clean_text(row.get("description")),
        "paid": _money(row.get("paid")),
        "reserve": _money(row.get("reserve")),
        "incurred": None,
        "status": _claim_status(row["status"]) if row.get("status") else None,
    }
    partial = dol is None
    if partial:
        data = {"claim_number": key, "status": data["status"]}
    return key, data, partial


def _vehicle(row: dict):
    vin = (row.get("vin") or "").replace(" ", "").upper()
    if not vin:
        return None
    return vin, {
        "year": parse_int(row.get("year")) if row.get("year") else None,
        "make": clean_text(row.get("make")),
        "model": clean_text(row.get("model")),
        "vin": vin,
        "garaging_zip": clean_text(row.get("garaging_zip")),
        "cost_new": _money(row.get("cost_new")),
        "use": None,
    }, False


def _payroll(row: dict):
    code = (row.get("class_code") or "").strip()
    pay = _money(row.get("payroll"))
    if not code or pay is None:
        return None
    state = (row.get("state") or "").strip().upper() or None
    return f"{state or 'XX'}-{code}", {
        "state": state, "class_code": code, "description": clean_text(row.get("description")),
        "employees": parse_int(row.get("employees")) if row.get("employees") else None, "part_time": None, "payroll": pay,
    }, False


def _property(row: dict):
    addr = clean_text(row.get("address"))
    if not addr:
        return None
    return address_key(addr), {
        "address": addr, "city": clean_text(row.get("city")), "state": (row.get("state") or "").upper() or None,
        "zip": clean_text(row.get("zip")), "building_value": _money(row.get("building_value")),
        "contents_value": _money(row.get("contents_value")), "year_built": None, "construction": None,
    }, False


# --------------------------------------------------------------------------- #
# Provider clients
# --------------------------------------------------------------------------- #


class OpenAIJson:
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model = model
        self.model_version = f"openai:{model}"

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        kwargs: dict[str, Any] = {}
        if self.model.startswith("gpt-4"):
            kwargs["temperature"] = 0
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "submission_extraction", "schema": schema, "strict": True}},
            **kwargs,
        )
        return json.loads(resp.choices[0].message.content or "{}")


class GeminiJson:
    def __init__(self, api_key: str, model: str):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.model_version = f"gemini:{model}"

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        from google.genai import types

        resp = self._client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
                temperature=0,
            ),
        )
        return json.loads(resp.text or "{}")


def client_from_settings(settings: Settings) -> JsonLLM | None:
    provider = settings.llm_provider
    if provider in ("openai", "auto") and settings.openai_api_key:
        return OpenAIJson(settings.openai_api_key, settings.openai_model)
    if provider in ("gemini", "auto") and settings.gemini_api_key:
        return GeminiJson(settings.gemini_api_key, settings.gemini_model)
    return None
