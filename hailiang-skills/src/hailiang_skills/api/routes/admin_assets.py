from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def build_admin_assets_router() -> APIRouter:
    router = APIRouter()

    @router.get("/assets/versions")
    def get_asset_versions() -> dict:
        root = PROJECT_ROOT / "assets" / "generated"
        manifests = []
        for manifest in root.glob("**/asset_manifest.json"):
            manifests.append({"path": str(manifest.relative_to(PROJECT_ROOT))})
        return {"manifests": manifests}

    return router
