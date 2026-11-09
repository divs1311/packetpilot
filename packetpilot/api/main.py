"""REST API.

    uvicorn packetpilot.api.main:app --reload

Interactive docs at /docs. If ``PACKETPILOT_API_KEY`` is set, every /api call
must send it in the ``X-API-Key`` header. The reviewer's identity is sent as
``X-Reviewer`` (wire this to your SSO in production).
"""

from __future__ import annotations

from datetime import date
from email.message import EmailMessage
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .. import __version__
from ..config import get_settings
from ..pipeline import InputFile
from ..schema import FIELD_SPECS
from ..service import PacketPilotService, ServiceError
from ..worker import enqueue

app = FastAPI(
    title="PacketPilot API",
    version=__version__,
    description="Evidence-backed submission intake for commercial insurance brokerages. "
    "The model proposes, deterministic rules validate, a human approves.",
)

_service: PacketPilotService | None = None


def service() -> PacketPilotService:
    global _service
    if _service is None:
        _service = PacketPilotService()
    return _service


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    key = get_settings().api_key
    if key and x_api_key != key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def reviewer(x_reviewer: str | None = Header(default=None)) -> str:
    return (x_reviewer or "anonymous").strip()[:120]


@app.exception_handler(ServiceError)
async def _service_error(_: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.extra})


# ---------------------------------------------------------------------------- models


class FieldReview(BaseModel):
    action: Literal["accept", "correct", "escalate"]
    value: Any | None = None
    candidate_id: str | None = None
    note: str | None = Field(None, max_length=2000)
    expected_version: int | None = None


class IssueAction(BaseModel):
    action: Literal["resolve", "waive", "reopen"]
    note: str | None = Field(None, max_length=2000)
    expected_version: int | None = None


class RecordAction(BaseModel):
    action: Literal["accept", "reject", "restore"]
    note: str | None = Field(None, max_length=2000)
    expected_version: int | None = None


class VersionOnly(BaseModel):
    expected_version: int | None = None


# ---------------------------------------------------------------------------- routes


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "version": __version__, "queue": bool(s.redis_url), "extractor": s.extractor}


@app.get("/api/schema/fields", dependencies=[Depends(require_key)])
def field_catalogue() -> list[dict]:
    return [{"path": f.path, "label": f.label, "kind": f.kind, "critical": f.critical, "required_for": list(f.required_for)}
            for f in FIELD_SPECS.values()]


async def _files(files: list[UploadFile], email_text: str | None) -> list[InputFile]:
    out = []
    for f in files or []:
        data = await f.read()
        if data:
            out.append(InputFile(f.filename or "upload", data, f.content_type))
    if email_text and email_text.strip():
        msg = EmailMessage()
        msg["Subject"] = "Pasted email"
        msg.set_content(email_text)
        out.append(InputFile("pasted_email.eml", msg.as_bytes(), "message/rfc822"))
    return out


def _dispatch(sid: str, background: BackgroundTasks) -> str:
    if enqueue(sid):
        return "queued"
    if get_settings().process_mode == "sync":
        service().process(sid)
        return "processed"
    background.add_task(_process_quietly, sid)
    return "processing"


def _process_quietly(sid: str) -> None:
    try:
        service().process(sid)
    except Exception:  # failure is recorded on the submission row
        pass


@app.post("/api/submissions", status_code=202, dependencies=[Depends(require_key)])
async def create_submission(
    background: BackgroundTasks,
    files: list[UploadFile] = File(default=[]),
    email_text: str | None = Form(default=None),
    as_of: date | None = Form(default=None),
    who: str = Depends(reviewer),
) -> dict:
    inputs = await _files(files, email_text)
    sid = service().create_submission(inputs, actor=who, as_of=as_of)
    return {"id": sid, "dispatch": _dispatch(sid, background), **service().get(sid)}


@app.post("/api/submissions/{sid}/files", status_code=202, dependencies=[Depends(require_key)])
async def add_files(
    sid: str,
    background: BackgroundTasks,
    files: list[UploadFile] = File(default=[]),
    email_text: str | None = Form(default=None),
    who: str = Depends(reviewer),
) -> dict:
    inputs = await _files(files, email_text)
    if not inputs:
        raise HTTPException(400, "No files supplied")
    service().add_files(sid, inputs, who)
    return {"id": sid, "dispatch": _dispatch(sid, background)}


@app.post("/api/submissions/{sid}/reprocess", status_code=202, dependencies=[Depends(require_key)])
def reprocess(sid: str, background: BackgroundTasks) -> dict:
    service().get(sid)  # 404 check
    return {"id": sid, "dispatch": _dispatch(sid, background)}


@app.get("/api/submissions", dependencies=[Depends(require_key)])
def list_submissions(status: str | None = None, limit: int = Query(200, le=1000)) -> list[dict]:
    return service().list(status, limit)


@app.get("/api/submissions/{sid}", dependencies=[Depends(require_key)])
def get_submission(sid: str) -> dict:
    return service().get(sid)


@app.post("/api/submissions/{sid}/fields/approve-verified", dependencies=[Depends(require_key)])
def approve_verified(sid: str, body: VersionOnly | None = None, who: str = Depends(reviewer)) -> dict:
    return service().approve_verified(sid, who, body.expected_version if body else None)


@app.post("/api/submissions/{sid}/fields/{path}", dependencies=[Depends(require_key)])
def review_field(sid: str, path: str, body: FieldReview, who: str = Depends(reviewer)) -> dict:
    return service().review_field(sid, path, body.action, who, value=body.value, candidate_id=body.candidate_id,
                                  note=body.note, expected_version=body.expected_version)


@app.post("/api/submissions/{sid}/issues/{issue_id}", dependencies=[Depends(require_key)])
def issue_action(sid: str, issue_id: str, body: IssueAction, who: str = Depends(reviewer)) -> dict:
    return service().issue_action(sid, issue_id, body.action, who, body.note, body.expected_version)


@app.post("/api/submissions/{sid}/records/{record_id}", dependencies=[Depends(require_key)])
def record_action(sid: str, record_id: str, body: RecordAction, who: str = Depends(reviewer)) -> dict:
    return service().record_action(sid, record_id, body.action, who, body.note, body.expected_version)


@app.get("/api/submissions/{sid}/followup", dependencies=[Depends(require_key)])
def followup(sid: str) -> dict:
    return service().followup(sid)


@app.post("/api/submissions/{sid}/request-info", dependencies=[Depends(require_key)])
def request_info(sid: str, who: str = Depends(reviewer)) -> dict:
    return service().request_info(sid, who)


@app.post("/api/submissions/{sid}/mark-ready", dependencies=[Depends(require_key)])
def mark_ready(sid: str, body: VersionOnly | None = None, who: str = Depends(reviewer)) -> dict:
    return service().mark_ready(sid, who, body.expected_version if body else None)


@app.get("/api/submissions/{sid}/export", dependencies=[Depends(require_key)])
def export(sid: str) -> dict:
    return service().export(sid)


@app.get("/api/submissions/{sid}/audit", dependencies=[Depends(require_key)])
def audit(sid: str) -> list[dict]:
    return service().audit(sid)


@app.get("/api/submissions/{sid}/documents/{doc_id}", dependencies=[Depends(require_key)])
def document_pages(sid: str, doc_id: str) -> dict:
    return service().document_pages(sid, doc_id)


@app.get("/api/submissions/{sid}/documents/{doc_id}/pages/{page}.png", dependencies=[Depends(require_key)])
def page_png(sid: str, doc_id: str, page: int, x0: float | None = None, y0: float | None = None,
             x1: float | None = None, y1: float | None = None) -> Response:
    bbox = [x0, y0, x1, y1] if None not in (x0, y0, x1, y1) else None
    return Response(service().page_image(sid, doc_id, page, bbox), media_type="image/png")


@app.get("/api/submissions/{sid}/documents/{doc_id}/original", dependencies=[Depends(require_key)])
def original(sid: str, doc_id: str) -> Response:
    name, data, ctype = service().original_file(sid, doc_id)
    return Response(data, media_type=ctype or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/metrics", dependencies=[Depends(require_key)])
def metrics() -> dict:
    return service().metrics()
