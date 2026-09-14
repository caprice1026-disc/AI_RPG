"""FastAPI application factory。"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """副作用なくHTTP applicationを構築する。"""

    app = FastAPI(title="AI RPG API", version="0.1.0")

    @app.get("/health", tags=["運用"])
    async def health() -> dict[str, str]:
        """processがHTTP requestを処理できることを返す。"""

        return {"status": "ok"}

    return app
