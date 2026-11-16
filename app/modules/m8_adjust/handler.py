from fastapi import APIRouter
from fastapi.responses import Response
from app import catalog as registry
from app.errors import DomainError, http_error
from app.modules.m8_adjust import service

router = APIRouter()


@router.get("/api/catalogs")
def catalog_versions():
    active, items = registry.versions()
    return {"active": active, "versions": items, "events": registry.events()}


@router.post("/api/catalogs/{version}/activate")
def activate_catalog(version: str, body: dict | None = None):
    try:
        return registry.activate(version, (body or {}).get("expected_version"))
    except DomainError as exc:
        raise http_error(exc)


@router.get("/api/adjust")
def adjust_overview(base_run: str | None = None):
    active, items = registry.versions()
    return {"active": active, "catalogs": items, "adjustments": service.listing(base_run)}


@router.get("/api/adjustments")
def adjustments(base_run: str | None = None):
    return {"items": service.listing(base_run)}


@router.post("/api/runs/{run_id}/adjustments")
def create_adjustment(run_id: str, options: dict | None = None):
    try:
        return service.create(run_id, options or {})
    except DomainError as exc:
        raise http_error(exc)


@router.get("/api/adjustments/{adjustment_id}")
def show_adjustment(adjustment_id: str):
    try:
        return service.load(adjustment_id)
    except DomainError as exc:
        raise http_error(exc)


@router.patch("/api/adjustments/{adjustment_id}/review")
def review_adjustment(adjustment_id: str, options: dict):
    try:
        return service.review(adjustment_id, options.get("revision"), options.get("decision"))
    except DomainError as exc:
        raise http_error(exc)


@router.get("/api/adjustments/{adjustment_id}/evidence.csv")
def adjustment_evidence(adjustment_id: str):
    try:
        body = service.adjustment_evidence(service.load(adjustment_id))
        return Response(
            body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={adjustment_id}-evidence.csv"},
        )
    except DomainError as exc:
        raise http_error(exc)
