import os
from fastapi import APIRouter
from sqlalchemy import text

from app.database import DbSession
from app.version import __version__

router = APIRouter()


@router.get("/health")
def health(db: DbSession):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"

    return {
        "service": "training-api",
        "version": __version__,
        "database": db_status,
        "source_revision": os.environ.get("SOURCE_REVISION", "unknown"),
        "source_tree_hash": os.environ.get("SOURCE_TREE_HASH", "unknown"),
    }
