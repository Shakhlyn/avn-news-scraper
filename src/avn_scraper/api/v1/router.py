from fastapi import APIRouter

from .endpoints import accepted, health, news, runs, scrape

api_router = APIRouter(prefix="/api/v1")
for module in (news, accepted, scrape, runs, health):
    api_router.include_router(module.router)
