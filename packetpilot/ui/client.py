"""One interface, two transports: in-process (embedded) or HTTP to the FastAPI service."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..pipeline import InputFile


class ClientError(Exception):
    def __init__(self, message: str, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.extra = extra or {}


class LocalClient:
    mode = "embedded"

    def __init__(self, service=None):
        from ..service import PacketPilotService

        self.svc = service or PacketPilotService()

    def _call(self, fn, *a, **kw):
        from ..service import ServiceError

        try:
            return fn(*a, **kw)
        except ServiceError as e:
            raise ClientError(e.message, e.extra) from e

    def list(self, status=None):
        return self._call(self.svc.list, status)

    def get(self, sid):
        return self._call(self.svc.get, sid)

    def create(self, files: list[InputFile], reviewer: str, as_of: date | None = None, email_text: str | None = None):
        if email_text and email_text.strip():
            from email.message import EmailMessage

            msg = EmailMessage()
            msg["Subject"] = "Pasted email"
            msg.set_content(email_text)
            files = [*files, InputFile("pasted_email.eml", msg.as_bytes(), "message/rfc822")]
        sid = self._call(self.svc.create_submission, files, reviewer, as_of)
        try:
            self.svc.process(sid)
        except Exception as e:  # recorded on the row
            raise ClientError(f"Processing failed: {e}") from e
        return sid

    def add_files(self, sid, files, reviewer):
        self._call(self.svc.add_files, sid, files, reviewer)
        self.svc.process(sid)

    def reprocess(self, sid):
        self.svc.process(sid)

    def review_field(self, sid, path, action, reviewer, **kw):
        return self._call(self.svc.review_field, sid, path, action, reviewer, **kw)

    def approve_verified(self, sid, reviewer, expected_version=None):
        return self._call(self.svc.approve_verified, sid, reviewer, expected_version)

    def issue_action(self, sid, issue_id, action, reviewer, note=None, expected_version=None):
        return self._call(self.svc.issue_action, sid, issue_id, action, reviewer, note, expected_version)

    def record_action(self, sid, record_id, action, reviewer, note=None, expected_version=None):
        return self._call(self.svc.record_action, sid, record_id, action, reviewer, note, expected_version)

    def followup(self, sid):
        return self._call(self.svc.followup, sid)

    def request_info(self, sid, reviewer):
        return self._call(self.svc.request_info, sid, reviewer)

    def mark_ready(self, sid, reviewer, expected_version=None):
        return self._call(self.svc.mark_ready, sid, reviewer, expected_version)

    def export(self, sid):
        return self._call(self.svc.export, sid)

    def audit(self, sid):
        return self._call(self.svc.audit, sid)

    def document_pages(self, sid, doc_id):
        return self._call(self.svc.document_pages, sid, doc_id)

    def page_png(self, sid, doc_id, page, bbox=None):
        return self._call(self.svc.page_image, sid, doc_id, page, bbox)

    def metrics(self):
        return self._call(self.svc.metrics)


class HttpClient:
    mode = "api"

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 120):
        import httpx

        if "://" not in base_url:  # e.g. Render's internal "host:port"
            base_url = f"http://{base_url}"
        headers = {"X-API-Key": api_key} if api_key else {}
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    def _req(self, method: str, url: str, reviewer: str | None = None, **kw) -> Any:
        headers = kw.pop("headers", {})
        if reviewer:
            headers["X-Reviewer"] = reviewer
        r = self.http.request(method, url, headers=headers, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"detail": r.text}
            detail = body.pop("detail", r.text) if isinstance(body, dict) else str(body)
            raise ClientError(str(detail), body if isinstance(body, dict) else {})
        ctype = r.headers.get("content-type", "")
        return r.json() if "json" in ctype else r.content

    def list(self, status=None):
        return self._req("GET", "/api/submissions", params={"status": status} if status else None)

    def get(self, sid):
        return self._req("GET", f"/api/submissions/{sid}")

    def create(self, files, reviewer, as_of=None, email_text=None):
        multipart = [("files", (f.filename, f.data, f.content_type or "application/octet-stream")) for f in files]
        data = {}
        if as_of:
            data["as_of"] = as_of.isoformat()
        if email_text:
            data["email_text"] = email_text
        return self._req("POST", "/api/submissions", reviewer, files=multipart, data=data)["id"]

    def add_files(self, sid, files, reviewer):
        multipart = [("files", (f.filename, f.data, f.content_type or "application/octet-stream")) for f in files]
        return self._req("POST", f"/api/submissions/{sid}/files", reviewer, files=multipart)

    def reprocess(self, sid):
        return self._req("POST", f"/api/submissions/{sid}/reprocess")

    def review_field(self, sid, path, action, reviewer, **kw):
        return self._req("POST", f"/api/submissions/{sid}/fields/{path}", reviewer, json={"action": action, **kw})

    def approve_verified(self, sid, reviewer, expected_version=None):
        return self._req("POST", f"/api/submissions/{sid}/fields/approve-verified", reviewer,
                         json={"expected_version": expected_version})

    def issue_action(self, sid, issue_id, action, reviewer, note=None, expected_version=None):
        return self._req("POST", f"/api/submissions/{sid}/issues/{issue_id}", reviewer,
                         json={"action": action, "note": note, "expected_version": expected_version})

    def record_action(self, sid, record_id, action, reviewer, note=None, expected_version=None):
        return self._req("POST", f"/api/submissions/{sid}/records/{record_id}", reviewer,
                         json={"action": action, "note": note, "expected_version": expected_version})

    def followup(self, sid):
        return self._req("GET", f"/api/submissions/{sid}/followup")

    def request_info(self, sid, reviewer):
        return self._req("POST", f"/api/submissions/{sid}/request-info", reviewer)

    def mark_ready(self, sid, reviewer, expected_version=None):
        return self._req("POST", f"/api/submissions/{sid}/mark-ready", reviewer, json={"expected_version": expected_version})

    def export(self, sid):
        return self._req("GET", f"/api/submissions/{sid}/export")

    def audit(self, sid):
        return self._req("GET", f"/api/submissions/{sid}/audit")

    def document_pages(self, sid, doc_id):
        return self._req("GET", f"/api/submissions/{sid}/documents/{doc_id}")

    def page_png(self, sid, doc_id, page, bbox=None):
        params = dict(zip(("x0", "y0", "x1", "y1"), bbox)) if bbox else None
        return self._req("GET", f"/api/submissions/{sid}/documents/{doc_id}/pages/{page}.png", params=params)

    def metrics(self):
        return self._req("GET", "/api/metrics")
