from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from contentai.services.readiness import check_api_readiness

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    is_started = bool(getattr(request.app.state, "ready", False))
    dependencies_ready, checks = check_api_readiness(request.app.state.settings)
    is_ready = is_started and dependencies_ready
    return JSONResponse(
        status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ready" if is_ready else "not_ready",
            "checks": checks,
        },
    )
