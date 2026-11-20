"""REST API (FastAPI). Thin: validation, correlation ids and error mapping only."""
from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.container import Container, build_container
from app.domain.errors import DomainError
from app.ingest.indexer import IngestOptions, run_ingestion
from app.observability import configure_logging, correlation_id_var, log, new_correlation_id

logger = logging.getLogger("api")


class AskRequest(BaseModel):
    user_id: str = Field(..., examples=["U2001"], description="Staff user id (see staff_users.csv). "
                         "In production this comes from the SSO token, not the body.")
    question: str = Field(..., min_length=1, max_length=1000, examples=["What is the procedure for a needle-stick injury?"])


class CitationOut(BaseModel):
    sop_id: str
    version: str
    title: str
    section: str
    section_title: str
    page: int


class AskResponse(BaseModel):
    status: Literal["answered", "not_found"]
    answer: str
    citations: list[CitationOut]
    conflict: bool = Field(description="True when the cited SOPs give different rules for the same thing.")
    cached: bool
    index_generation: int
    correlation_id: str


class ErrorBody(BaseModel):
    code: str = Field(examples=["patient_data_detected"])
    message: str
    details: dict = {}
    correlation_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class ReindexRequest(BaseModel):
    snapshot_dir: str = Field(..., examples=["data/sop_updates/2026-11-01"])
    dry_run: bool = False


ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "invalid_question / ingestion_error / validation_error"},
    403: {"model": ErrorResponse, "description": "unknown_user"},
    422: {"model": ErrorResponse, "description": "patient_data_detected"},
    503: {"model": ErrorResponse, "description": "upstream_unavailable / index_not_ready"},
}


def create_app(container: Container | None = None) -> FastAPI:
    app = FastAPI(title="MediCare SOP Assistant", version="1.0.0",
                  description="Answers staff questions from the current SOPs only, with citations.")
    state: dict = {"c": container}

    def c() -> Container:
        if state["c"] is None:
            s = Settings.from_env()
            configure_logging(s.log_level)
            state["c"] = build_container(s)
        return state["c"]

    def err(code: str, message: str, status: int, details: dict | None = None) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {
            "code": code, "message": message, "details": details or {}, "correlation_id": correlation_id_var.get()}})

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or new_correlation_id()
        token = correlation_id_var.set(cid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["x-correlation-id"] = cid
            log(logger, "http.request", method=request.method, path=request.url.path,
                status=response.status_code, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            return response
        finally:
            correlation_id_var.reset(token)

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, e: DomainError):
        return err(e.code, e.message, e.http_status, e.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError):
        # Don't echo the input back: it may contain patient data.
        return err("validation_error", "Request body is invalid.", 400,
                   {"fields": [".".join(str(p) for p in x["loc"]) for x in e.errors()]})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, e: Exception):
        logger.exception("unhandled error")
        return err("internal_error", "Unexpected error.", 500)

    @app.get("/healthz")
    def healthz():
        cont = c()
        snap = cont.index.snapshot()
        return {"status": "ok", "index_generation": snap.generation, "chunks": len(snap.chunks),
                "documents": len({ch.sop_id for ch in snap.chunks}), "provider": cont.settings.provider}

    @app.post("/v1/ask", response_model=AskResponse, responses=ERROR_RESPONSES)
    def ask(req: AskRequest):
        cid = correlation_id_var.get()
        ans, _ = c().answers.ask(req.user_id, req.question, correlation_id=cid)
        return AskResponse(
            status=ans.status.value, answer=ans.answer,
            citations=[CitationOut(sop_id=x.sop_id, version=x.version, title=x.title, section=x.section,
                                   section_title=x.section_title, page=x.page) for x in ans.citations],
            conflict=ans.conflict, cached=ans.cached, index_generation=ans.index_generation, correlation_id=cid)

    @app.post("/v1/admin/reindex", responses=ERROR_RESPONSES)
    def reindex(req: ReindexRequest):
        """Admin-only in production (network-restricted / IAM). Runs a snapshot ingestion."""
        cont = c()
        return run_ingestion(req.snapshot_dir, cont.db, cont.embedder, cont.policy, IngestOptions(dry_run=req.dry_run))

    @app.get("/v1/documents")
    def documents(user_id: str):
        """SOPs the given user may read (titles of restricted SOPs are not disclosed to others)."""
        cont = c()
        levels = cont.policy.levels_for_role(cont.users.get(user_id).role)
        return [{"sop_id": r["sop_id"], "version": r["version"], "title": r["title"],
                 "effective_date": r["effective_date"]}
                for r in cont.db.active_documents().values() if r["access_level"] in levels]

    return app


app = create_app()
