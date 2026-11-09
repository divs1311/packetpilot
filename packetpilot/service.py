"""Application service: intake, processing, human review, readiness, analytics.

Both the FastAPI layer and the Streamlit UI (in embedded mode) call this
class, so every business rule - including "a human must approve" and the
audit trail - lives in exactly one place.
"""

from __future__ import annotations

import hashlib
import io
import re
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from .config import Settings, get_settings
from .db import AuditEvent, DocumentPagesRow, FieldRow, IssueRow, SubmissionRow, UploadRow, session_scope
from .ingest import ParsedDocument
from .normalize import normalize
from .pipeline import InputFile, make_reference, process_submission, reevaluate
from .readiness import assess, draft_followup
from .rules_engine import ISSUE_CATALOG
from .schema import FIELD_SPECS, REVIEWED_STATUSES, SubmissionState, utcnow

FIELD_DRIVEN_CODES = {"FIELD_CONFLICT", "EXTRACTION_DISAGREEMENT", "UNGROUNDED_VALUE", "LOW_CONFIDENCE",
                      "UNPARSEABLE_VALUE", "FIELD_ESCALATED"}


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.message = message
        self.extra = extra


class NotFound(ServiceError):
    status_code = 404


class VersionConflict(ServiceError):
    status_code = 409


class NotReady(ServiceError):
    status_code = 409


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:180] or "file"


class PacketPilotService:
    def __init__(self, settings: Settings | None = None, llm_client=None):
        self.settings = settings or get_settings()
        self.llm_client = llm_client
        self.db_url = self.settings.database_url

    # ------------------------------------------------------------------ helpers
    def _session(self):
        return session_scope(self.db_url)

    def _row(self, s, sid: str) -> SubmissionRow:
        row = s.get(SubmissionRow, sid)
        if row is None:
            row = s.scalar(select(SubmissionRow).where(SubmissionRow.reference == sid))
        if row is None:
            raise NotFound(f"Submission {sid} not found")
        return row

    @staticmethod
    def _state(row: SubmissionRow) -> SubmissionState:
        if not row.state_json:
            raise ServiceError("Submission has not been processed yet", status=row.status)
        st = SubmissionState.model_validate(row.state_json)
        st.version = row.version
        return st

    def _audit(self, s, sid: str, actor: str, action: str, *, target_type=None, target_id=None,
               before=None, after=None, note=None, model_version=None) -> None:
        s.add(AuditEvent(submission_id=sid, actor=actor, action=action, target_type=target_type,
                         target_id=target_id, before_json=before, after_json=after, note=note,
                         model_version=model_version, created_at=utcnow()))

    def _save(self, s, row: SubmissionRow, state: SubmissionState) -> None:
        row.version = (row.version or 0) + 1
        state.version = row.version
        row.state_json = state.model_dump(mode="json")
        row.status = state.status
        row.applicant_name = state.value("applicant.legal_name") or row.applicant_name
        row.updated_at = utcnow()
        s.execute(delete(FieldRow).where(FieldRow.submission_id == row.id))
        s.execute(delete(IssueRow).where(IssueRow.submission_id == row.id))
        for path, f in state.fields.items():
            sel = f.selected
            s.add(FieldRow(
                submission_id=row.id, path=path, value_json=f.value, status=f.status, confidence=f.confidence,
                source_document=sel.evidence.filename if sel else None, source_page=sel.evidence.page if sel else None,
                method=sel.evidence.method if sel else None, reviewed_by=f.reviewed_by, reviewed_at=f.reviewed_at,
            ))
        for i in state.issues:
            s.add(IssueRow(
                submission_id=row.id, issue_id=i.id, code=i.code, severity=i.severity, category=i.category,
                status=i.status, field_path=i.field_path, subject=i.subject, message=i.message,
                first_seen_at=i.first_seen_at, resolved_at=i.resolved_at, resolved_by=i.resolved_by,
                auto_resolved=i.auto_resolved,
            ))

    def _mutable(self, s, sid: str, expected_version: int | None, actor: str) -> tuple[SubmissionRow, SubmissionState]:
        row = self._row(s, sid)
        if expected_version is not None and expected_version != row.version:
            raise VersionConflict(
                f"Submission changed since you loaded it (version {expected_version} -> {row.version}). Reload and retry.",
                current_version=row.version,
            )
        state = self._state(row)
        if row.first_review_at is None:
            row.first_review_at = utcnow()
        if state.status == "ready":
            state.status = "needs_review"
            row.ready_at = None
            self._audit(s, row.id, actor, "reopened", note="Edited after being marked ready")
        return row, state

    def _store(self, s, sid: str, files: list[InputFile], actor: str) -> None:
        use_fs = self.settings.storage_backend == "fs"
        base = Path(self.settings.storage_dir) / sid
        for f in files:
            sha = hashlib.sha256(f.data).hexdigest()
            path = None
            if use_fs:
                base.mkdir(parents=True, exist_ok=True)
                path = base / f"{sha[:12]}_{_safe_name(f.filename)}"
                path.write_bytes(f.data)
            s.add(UploadRow(submission_id=sid, filename=_safe_name(f.filename), content_type=f.content_type,
                            sha256=sha, size=len(f.data), storage_path=str(path) if path else None,
                            data=None if use_fs else f.data, uploaded_by=actor))
            self._audit(s, sid, actor, "file_uploaded", target_type="upload", target_id=_safe_name(f.filename),
                        after={"sha256": sha, "size": len(f.data)})

    # ------------------------------------------------------------------ intake
    def create_submission(self, files: list[InputFile], actor: str, as_of: date | None = None,
                          source: str = "upload") -> str:
        if not files:
            raise ServiceError("Upload at least one document")
        sid = uuid.uuid4().hex
        as_of = as_of or date.today()
        with self._session() as s:
            ref = make_reference(sid, as_of)
            s.add(SubmissionRow(id=sid, reference=ref, status="received", as_of=as_of, source=source,
                                created_by=actor, created_at=utcnow(), version=0))
            s.flush()
            self._audit(s, sid, actor, "submission_created", note=f"{len(files)} file(s)")
            self._store(s, sid, files, actor)
        return sid

    def add_files(self, sid: str, files: list[InputFile], actor: str) -> None:
        with self._session() as s:
            row = self._row(s, sid)
            self._store(s, row.id, files, actor)
            if row.status in ("awaiting_info", "ready"):
                row.status = "received"
                row.ready_at = None

    def process(self, sid: str, actor: str = "system") -> SubmissionState:
        with self._session() as s:
            row = self._row(s, sid)
            uploads = s.scalars(select(UploadRow).where(UploadRow.submission_id == row.id).order_by(UploadRow.id)).all()
            previous = self._state(row) if row.state_json else None
            row.status = "processing"
            row_id, as_of, ref = row.id, row.as_of, row.reference
            files = [InputFile(u.filename, _upload_bytes(u), u.content_type) for u in uploads]
            upload_by_sha = {u.sha256: u.id for u in uploads}
        try:
            out = process_submission(files, submission_id=row_id, reference=ref, as_of=as_of,
                                     settings=self.settings, llm_client=self.llm_client, previous=previous)
        except Exception as exc:
            with self._session() as s:
                row = self._row(s, row_id)
                row.status, row.error = "failed", f"{exc.__class__.__name__}: {exc}"
                self._audit(s, row_id, actor, "processing_failed", note=row.error)
            raise
        state = out.state
        with self._session() as s:
            row = self._row(s, row_id)
            row.processed_at = utcnow()
            row.processing_ms = state.processing_ms
            row.extractor, row.model_version, row.error = state.extractor, state.model_version, None
            s.execute(delete(DocumentPagesRow).where(DocumentPagesRow.submission_id == row_id))
            for doc in state.documents:
                parsed = out.parsed.get(doc.id)
                s.add(DocumentPagesRow(document_id=doc.id, submission_id=row_id, filename=doc.filename,
                                       kind=parsed.kind if parsed else "unknown",
                                       upload_id=upload_by_sha.get(doc.sha256), pages_json=_pages_json(parsed)))
            self._save(s, row, state)
            n_open = sum(1 for i in state.issues if i.status == "open")
            self._audit(s, row_id, actor, "processed", model_version=state.model_version,
                        after={"documents": len(state.documents), "open_issues": n_open, "ms": state.processing_ms,
                               "extractor": state.extractor})
        return state

    # ------------------------------------------------------------------ reads
    def get(self, sid: str) -> dict:
        with self._session() as s:
            row = self._row(s, sid)
            base = self._summary(row)
            if not row.state_json:
                return {**base, "state": None, "readiness": None}
            state = self._state(row)
            return {**base, "state": state.model_dump(mode="json"), "readiness": assess(state).as_dict()}

    def state(self, sid: str) -> SubmissionState:
        with self._session() as s:
            return self._state(self._row(s, sid))

    def _summary(self, row: SubmissionRow) -> dict:
        st = row.state_json or {}
        issues = st.get("issues", [])
        open_blocking = sum(1 for i in issues if i["status"] == "open" and i["severity"] in ("critical", "warning"))
        fields = st.get("fields", {})
        pending = sum(1 for f in fields.values() if f.get("value") is not None and f.get("status") not in REVIEWED_STATUSES)
        created = _aware(row.created_at)
        age_h = (utcnow() - created).total_seconds() / 3600 if created else None
        return {
            "id": row.id, "reference": row.reference, "applicant_name": row.applicant_name, "status": row.status,
            "as_of": row.as_of.isoformat(), "created_at": created.isoformat() if created else None,
            "ready_at": _aware(row.ready_at).isoformat() if row.ready_at else None,
            "documents": len(st.get("documents", [])), "open_blocking_issues": open_blocking,
            "fields_pending_review": pending, "version": row.version, "extractor": row.extractor,
            "model_version": row.model_version, "processing_ms": row.processing_ms, "error": row.error,
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "sla_breached": bool(age_h is not None and age_h > self.settings.sla_hours and row.status != "ready"),
            "info_requests": row.info_requests,
        }

    def list(self, status: str | None = None, limit: int = 200) -> list[dict]:
        with self._session() as s:
            q = select(SubmissionRow).order_by(SubmissionRow.created_at.desc()).limit(limit)
            if status:
                q = q.where(SubmissionRow.status == status)
            return [self._summary(r) for r in s.scalars(q).all()]

    def audit(self, sid: str) -> list[dict]:
        with self._session() as s:
            row = self._row(s, sid)
            events = s.scalars(select(AuditEvent).where(AuditEvent.submission_id == row.id).order_by(AuditEvent.id)).all()
            return [
                {"id": e.id, "at": _aware(e.created_at).isoformat(), "actor": e.actor, "action": e.action,
                 "target_type": e.target_type, "target_id": e.target_id, "before": e.before_json, "after": e.after_json,
                 "note": e.note, "model_version": e.model_version}
                for e in events
            ]

    def document_pages(self, sid: str, doc_id: str) -> dict:
        with self._session() as s:
            row = self._row(s, sid)
            d = s.get(DocumentPagesRow, doc_id)
            if d is None or d.submission_id != row.id:
                raise NotFound("Document not found")
            return {"document_id": d.document_id, "filename": d.filename, "kind": d.kind, "pages": d.pages_json,
                    "has_original": d.upload_id is not None}

    def original_file(self, sid: str, doc_id: str) -> tuple[str, bytes, str | None]:
        with self._session() as s:
            row = self._row(s, sid)
            d = s.get(DocumentPagesRow, doc_id)
            if d is None or d.submission_id != row.id or d.upload_id is None:
                raise NotFound("Original file not available")
            up = s.get(UploadRow, d.upload_id)
            return up.filename, _upload_bytes(up), up.content_type

    def page_image(self, sid: str, doc_id: str, page: int, bbox: list[float] | None = None, dpi: int = 110) -> bytes:
        """PNG of a PDF/image page with the evidence box highlighted."""
        fname, data, _ = self.original_file(sid, doc_id)
        from PIL import Image, ImageDraw

        if fname.lower().endswith(".pdf"):
            import pymupdf

            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                if not 1 <= page <= len(pdf):
                    raise NotFound("Page out of range")
                pix = pdf[page - 1].get_pixmap(dpi=dpi)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            scale = dpi / 72
        elif fname.lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff")):
            img = Image.open(io.BytesIO(data)).convert("RGB")
            src_dpi = img.info.get("dpi", (300, 300))[0] or 300
            scale = src_dpi / 72
        else:
            raise ServiceError("Page images are available for PDF and image files only")
        if bbox:
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            x0, y0, x1, y1 = (v * scale for v in bbox)
            draw.rectangle([x0 - 4, y0 - 3, x1 + 4, y1 + 3], fill=(255, 196, 0, 70), outline=(214, 120, 0, 255), width=3)
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    # ------------------------------------------------------------------ review
    def review_field(self, sid: str, path: str, action: str, actor: str, *, value: Any = None,
                     candidate_id: str | None = None, note: str | None = None,
                     expected_version: int | None = None) -> dict:
        if path not in FIELD_SPECS:
            raise NotFound(f"Unknown field {path}")
        if action not in ("accept", "correct", "escalate"):
            raise ServiceError("action must be accept, correct or escalate")
        spec = FIELD_SPECS[path]
        with self._session() as s:
            row, state = self._mutable(s, sid, expected_version, actor)
            f = state.fields[path]
            before = {"value": f.value, "status": f.status}
            if action == "accept":
                if candidate_id:
                    cand = next((c for c in f.candidates if c.id == candidate_id), None)
                    if cand is None:
                        raise NotFound("Candidate not found")
                    if cand.value is None:
                        raise ServiceError("That candidate has no readable value; use 'correct' instead")
                    f.value, f.selected_candidate_id = cand.value, cand.id
                if f.value is None:
                    raise ServiceError("Nothing to accept - this field has no value. Use 'correct' to enter one.")
                if f.status in ("conflict",) and not candidate_id and not note:
                    raise ServiceError("This field has conflicting sources. Pick a candidate or add a note explaining the choice.")
                f.status = "accepted"
            elif action == "correct":
                parsed = normalize(spec.kind, value)
                if parsed is None:
                    raise ServiceError(f"Could not interpret '{value}' as {spec.kind}")
                match = next((c for c in f.candidates if c.value == parsed), None)
                if match is None and not note:
                    raise ServiceError("A note is required when the corrected value is not supported by a source document "
                                       "(e.g. 'Confirmed with client by phone 11/09').")
                if f.original_value is None:
                    f.original_value = f.value
                f.value = parsed
                f.selected_candidate_id = match.id if match else None
                f.status = "corrected"
            else:
                if not note:
                    raise ServiceError("A note is required to escalate")
                f.status = "escalated"
            f.reviewed_by, f.reviewed_at, f.review_note = actor, utcnow(), note
            reevaluate(state, self.settings)
            self._save(s, row, state)
            self._audit(s, row.id, actor, f"field_{action}", target_type="field", target_id=path, before=before,
                        after={"value": f.value, "status": f.status}, note=note, model_version=state.model_version)
            return {"field": f.model_dump(mode="json"), "readiness": assess(state).as_dict(), "version": row.version}

    def approve_verified(self, sid: str, actor: str, expected_version: int | None = None) -> dict:
        with self._session() as s:
            row, state = self._mutable(s, sid, expected_version, actor)
            approved = []
            for path, f in state.fields.items():
                if f.status == "auto_verified":
                    f.status, f.reviewed_by, f.reviewed_at = "accepted", actor, utcnow()
                    f.review_note = "Bulk-approved (high confidence, grounded, consistent across sources)"
                    approved.append(path)
            reevaluate(state, self.settings)
            self._save(s, row, state)
            self._audit(s, row.id, actor, "fields_bulk_approved", target_type="field", after={"fields": approved},
                        model_version=state.model_version)
            return {"approved": approved, "readiness": assess(state).as_dict(), "version": row.version}

    def issue_action(self, sid: str, issue_id: str, action: str, actor: str, note: str | None = None,
                     expected_version: int | None = None) -> dict:
        if action not in ("resolve", "waive", "reopen"):
            raise ServiceError("action must be resolve, waive or reopen")
        with self._session() as s:
            row, state = self._mutable(s, sid, expected_version, actor)
            issue = next((i for i in state.issues if i.id == issue_id), None)
            if issue is None:
                raise NotFound("Issue not found")
            if action != "reopen" and issue.code in FIELD_DRIVEN_CODES:
                raise ServiceError(f"{issue.code} is cleared by reviewing the field ({issue.field_path}), not directly.")
            if action == "waive" and not note:
                raise ServiceError("A note is required to waive an issue")
            if action == "resolve" and issue.severity == "critical" and not note:
                raise ServiceError("A note is required to resolve a critical issue")
            before = issue.status
            if action == "reopen":
                issue.status, issue.resolved_by, issue.resolved_at, issue.resolution_note = "open", None, None, None
                issue.auto_resolved = False
            else:
                issue.status = "resolved" if action == "resolve" else "waived"
                issue.resolved_by, issue.resolved_at, issue.resolution_note = actor, utcnow(), note
                issue.auto_resolved = False
            reevaluate(state, self.settings)
            self._save(s, row, state)
            self._audit(s, row.id, actor, f"issue_{action}", target_type="issue", target_id=f"{issue.code}:{issue.id}",
                        before={"status": before}, after={"status": issue.status}, note=note)
            return {"issue": issue.model_dump(mode="json"), "readiness": assess(state).as_dict(), "version": row.version}

    def record_action(self, sid: str, record_id: str, action: str, actor: str, note: str | None = None,
                      expected_version: int | None = None) -> dict:
        if action not in ("accept", "reject", "restore"):
            raise ServiceError("action must be accept, reject or restore")
        with self._session() as s:
            row, state = self._mutable(s, sid, expected_version, actor)
            rec = next((r for r in state.records if r.id == record_id), None)
            if rec is None:
                raise NotFound("Record not found")
            if action == "reject" and not note:
                raise ServiceError("A note is required to reject a schedule row")
            before = rec.status
            rec.status = {"accept": "accepted", "reject": "rejected", "restore": "extracted"}[action]
            reevaluate(state, self.settings)
            self._save(s, row, state)
            self._audit(s, row.id, actor, f"record_{action}", target_type="record", target_id=f"{rec.collection}:{rec.key}",
                        before={"status": before}, after={"status": rec.status}, note=note)
            return {"record": rec.model_dump(mode="json"), "readiness": assess(state).as_dict(), "version": row.version}

    def followup(self, sid: str, broker_name: str = "New Business Team") -> dict:
        return draft_followup(self.state(sid), broker_name)

    def request_info(self, sid: str, actor: str, broker_name: str = "New Business Team") -> dict:
        with self._session() as s:
            row, state = self._mutable(s, sid, None, actor)
            draft = draft_followup(state, broker_name)
            if not draft["items"]:
                raise ServiceError("There are no open data issues to ask the client about")
            state.status = "awaiting_info"
            row.info_requests = (row.info_requests or 0) + 1
            self._save(s, row, state)
            self._audit(s, row.id, actor, "info_requested", after=draft)
            return draft

    def mark_ready(self, sid: str, actor: str, expected_version: int | None = None) -> dict:
        with self._session() as s:
            row, state = self._mutable(s, sid, expected_version, actor)
            r = assess(state)
            if not r.ready:
                raise NotReady("Submission is not ready", blockers=r.blockers)
            state.status = "ready"
            row.ready_at = utcnow()
            self._save(s, row, state)
            self._audit(s, row.id, actor, "marked_ready", after={"reference": row.reference})
            return {"status": "ready", "ready_at": row.ready_at.isoformat(), "version": row.version}

    def export(self, sid: str) -> dict:
        with self._session() as s:
            row = self._row(s, sid)
            state = self._state(row)
            fields = {}
            for path, f in state.fields.items():
                sel = f.selected
                fields[path] = {
                    "label": FIELD_SPECS[path].label, "value": f.value, "status": f.status,
                    "confidence": f.confidence,
                    "evidence": None if sel is None else {
                        "document": sel.evidence.filename, "page": sel.evidence.page, "locator": sel.evidence.locator,
                        "text": sel.evidence.text, "method": sel.evidence.method, "grounded": sel.evidence.grounded,
                    },
                    "reviewed_by": f.reviewed_by,
                    "reviewed_at": f.reviewed_at.isoformat() if f.reviewed_at else None,
                    "review_note": f.review_note,
                }
            schedules = defaultdict(list)
            for r in state.records:
                if r.status == "rejected":
                    continue
                schedules[r.collection].append({**r.data, "_source": f"{r.evidence.filename} p{r.evidence.page}",
                                                "_partial": r.partial})
            return {
                "schema": "packetpilot.submission/v1", "reference": state.reference, "status": state.status,
                "ready": state.status == "ready", "ready_at": _aware(row.ready_at).isoformat() if row.ready_at else None,
                "as_of": state.as_of.isoformat(), "extractor": state.extractor, "model_version": state.model_version,
                "documents": [{"filename": d.filename, "type": d.doc_type, "pages": d.page_count, "sha256": d.sha256}
                              for d in state.documents],
                "fields": fields, "schedules": dict(schedules),
                "issues": [{"code": i.code, "severity": i.severity, "status": i.status, "message": i.message,
                            "resolution_note": i.resolution_note, "resolved_by": i.resolved_by} for i in state.issues],
            }

    # ------------------------------------------------------------------ analytics
    def metrics(self) -> dict:
        now = utcnow()
        with self._session() as s:
            subs = s.scalars(select(SubmissionRow)).all()
            open_issues = s.execute(
                select(IssueRow.code, func.count()).where(IssueRow.status == "open").group_by(IssueRow.code)
            ).all()
            reviewed = s.execute(
                select(FieldRow.status, func.count()).where(FieldRow.status.in_(["accepted", "corrected"])).group_by(FieldRow.status)
            ).all()
            method_corr = s.execute(
                select(FieldRow.method, FieldRow.status, func.count())
                .where(FieldRow.status.in_(["accepted", "corrected"])).group_by(FieldRow.method, FieldRow.status)
            ).all()
            incomplete = s.scalar(
                select(func.count(func.distinct(IssueRow.submission_id))).where(
                    IssueRow.status == "open", IssueRow.code.in_(["MISSING_DOCUMENT", "MISSING_REQUIRED_FIELD", "INCOMPLETE_EXPLANATION"])
                )
            ) or 0
            actions = s.execute(
                select(AuditEvent.submission_id, func.count()).where(
                    AuditEvent.action.in_(["field_accept", "field_correct", "field_escalate", "fields_bulk_approved",
                                           "issue_resolve", "issue_waive", "record_reject", "record_accept"])
                ).group_by(AuditEvent.submission_id)
            ).all()

        by_status = Counter(r.status for r in subs)
        proc = [r.processing_ms for r in subs if r.processing_ms is not None]
        ready = [r for r in subs if r.status == "ready" and r.ready_at]
        ttr = [(_aware(r.ready_at) - _aware(r.created_at)).total_seconds() / 3600 for r in ready]
        review_min = [(_aware(r.ready_at) - _aware(r.first_review_at)).total_seconds() / 60 for r in ready if r.first_review_at]
        first_pass = [r for r in ready if (r.info_requests or 0) == 0]
        breached = [r for r in subs if r.status != "ready" and (now - _aware(r.created_at)).total_seconds() / 3600 > self.settings.sla_hours]
        rev = dict(reviewed)
        n_rev = sum(rev.values())
        intake = Counter(_aware(r.created_at).date().isoformat() for r in subs)
        last14 = [(now.date() - timedelta(days=d)).isoformat() for d in range(13, -1, -1)]
        corr_by_method: dict[str, dict[str, int]] = defaultdict(lambda: {"accepted": 0, "corrected": 0})
        for method, status, n in method_corr:
            # "llm:openai:gpt-x" -> "llm", "rules:text" -> "rules", None -> value typed by reviewer
            corr_by_method[(method or "reviewer").split(":")[0]][status] += n

        def med(xs):
            return round(statistics.median(xs), 2) if xs else None

        return {
            "total_submissions": len(subs),
            "by_status": dict(by_status),
            "outstanding": sum(by_status.get(k, 0) for k in ("received", "processing", "needs_review", "awaiting_info")),
            "incomplete_packages": incomplete,
            "sla_hours": self.settings.sla_hours,
            "sla_breaches": [{"reference": r.reference, "applicant": r.applicant_name, "status": r.status,
                              "age_hours": round((now - _aware(r.created_at)).total_seconds() / 3600, 1)} for r in breached],
            "median_processing_seconds": med([p / 1000 for p in proc]),
            "median_hours_to_ready": med(ttr),
            "median_review_minutes": med(review_min),
            "first_pass_ready_rate": round(len(first_pass) / len(ready), 3) if ready else None,
            "rework_rate": round(sum(1 for r in subs if (r.info_requests or 0) > 0) / len(subs), 3) if subs else None,
            "reviewed_fields": n_rev,
            "correction_rate": round(rev.get("corrected", 0) / n_rev, 3) if n_rev else None,
            "corrections_by_method": dict(corr_by_method),
            "mean_review_actions": round(statistics.mean(n for _, n in actions), 1) if actions else None,
            "open_issues_by_code": dict(sorted(open_issues, key=lambda kv: -kv[1])),
            "intake_last_14_days": [{"date": d, "count": intake.get(d, 0)} for d in last14],
        }


def _upload_bytes(u: UploadRow) -> bytes:
    if u.data is not None:
        return u.data
    if u.storage_path:
        return Path(u.storage_path).read_bytes()
    raise NotFound(f"Stored bytes for {u.filename} are missing")


def _pages_json(parsed: ParsedDocument | None) -> list:
    if parsed is None:
        return []
    return [
        {
            "number": p.number, "name": p.name, "ocr": p.ocr, "width": p.width, "height": p.height,
            "lines": [{"text": ln.text, "bbox": list(ln.bbox) if ln.bbox else None, "locator": ln.locator,
                       "confidence": ln.confidence} for ln in p.lines],
        }
        for p in parsed.pages
    ]


ISSUE_TITLES = {k: v[2] for k, v in ISSUE_CATALOG.items()}
