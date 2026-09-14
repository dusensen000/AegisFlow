"""FastAPI application with durable graph lifecycle and owned background runs."""

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.api.routes import build_router
from app.context import create_context
from app.graph import build_graph
from app.services.runs import RunManager
from app.services.server_lease import ServerLease
from app.services.chat import ChatService


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(context=None):
    context = context or create_context()
    @asynccontextmanager
    async def lifespan(app):
        with ServerLease(context.settings.database_path):
            await context.repository.open()
            try:
                await context.repository.recover()
                if hasattr(context.tools, "open"):
                    await context.tools.open()
                async with AsyncSqliteSaver.from_conn_string(context.settings.database_path + ".checkpoints") as saver:
                    context.graph = build_graph(context, saver)
                    app.state.runs = RunManager(context, context.repository)
                    app.state.chat = ChatService(context, app.state.runs)
                    await app.state.chat.recover()
                    try:
                        yield
                    finally:
                        await app.state.chat.close()
                        await app.state.runs.close()
            finally:
                await context.repository.close()
                if hasattr(context.llm, "aclose"):
                    await context.llm.aclose()
                if hasattr(context.tracer, "flush"):
                    context.tracer.flush()
    app = FastAPI(title="AegisFlow API", version="1.1.0", lifespan=lifespan)
    app.state.context = context
    if context.settings.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=context.settings.allowed_origins,
                           allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "Authorization", "Last-Event-ID"])
    app.include_router(build_router(context))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")
    return app


app = create_app()
