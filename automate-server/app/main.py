"""Mirrors src/server.js: FastAPI app setup, route mounting, /health."""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import config
from app.routes.eligible_vendors import router as eligible_vendors_router
from app.routes.runs import router as runs_router
from app.routes.scripts import router as scripts_router
from app.routes.self_heal_queue import router as self_heal_queue_router
from app.routes.vendors import router as vendors_router


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return JSONResponse(content={"ok": True})

    app.include_router(scripts_router, prefix="/api/scripts")
    app.include_router(vendors_router, prefix="/api/vendors")
    app.include_router(runs_router, prefix="/api/runs")
    app.include_router(self_heal_queue_router, prefix="/api/self-heal-queue")
    app.include_router(eligible_vendors_router, prefix="/api/eligible-vendors")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.port)
