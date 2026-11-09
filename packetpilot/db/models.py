"""Relational schema (PostgreSQL in production, SQLite locally).

The submission aggregate (``SubmissionState``) is stored as a versioned JSON
document on ``submissions`` - it is always read and written as a whole, and
optimistic locking on ``version`` prevents lost updates between reviewers.
``fields`` and ``issues`` are normalised projections rewritten on each save
so operations analytics are plain SQL. ``audit_events`` is append-only.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..schema import utcnow


class Base(DeclarativeBase):
    pass


class SubmissionRow(Base):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    applicant_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(24), default="upload")
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    extractor: Mapped[str | None] = mapped_column(String(24))
    model_version: Mapped[str | None] = mapped_column(String(120))
    info_requests: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    state_json: Mapped[dict | None] = mapped_column(JSON)


class UploadRow(Base):
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    # "db" storage keeps bytes in the database (works when API and workers run on
    # separate machines); "fs" storage keeps a path on shared disk.
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(120))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentPagesRow(Base):
    """Parsed text layout per document, used by the evidence viewer."""

    __tablename__ = "document_pages"

    document_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(24))
    upload_id: Mapped[int | None] = mapped_column(Integer)
    pages_json: Mapped[list] = mapped_column(JSON)


class FieldRow(Base):
    __tablename__ = "fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(80))
    value_json: Mapped[object | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_document: Mapped[str | None] = mapped_column(String(255))
    source_page: Mapped[int | None] = mapped_column(Integer)
    method: Mapped[str | None] = mapped_column(String(80))
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IssueRow(Base):
    __tablename__ = "issues"
    __table_args__ = (Index("ix_issues_sub_status", "submission_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[str] = mapped_column(String(32))
    code: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(12))
    category: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12))
    field_path: Mapped[str | None] = mapped_column(String(80))
    subject: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(120))
    auto_resolved: Mapped[bool] = mapped_column(default=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(48), index=True)
    target_type: Mapped[str | None] = mapped_column(String(24))
    target_id: Mapped[str | None] = mapped_column(String(120))
    before_json: Mapped[object | None] = mapped_column(JSON)
    after_json: Mapped[object | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
