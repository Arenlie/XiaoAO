from fastapi import APIRouter

from app.api.v1 import (
    asr,
    chat,
    conversations,
    controller_agents,
    controller_tools,
    controller_workflows,
    diagnostics,
    files,
    messages,
    profile,
    tasks,
)

router = APIRouter()
router.include_router(asr.router)
router.include_router(chat.router)
router.include_router(files.router)
router.include_router(tasks.router)
router.include_router(conversations.router)
router.include_router(messages.router)
router.include_router(profile.router)
router.include_router(diagnostics.router)
router.include_router(controller_agents.router)
router.include_router(controller_tools.router)
router.include_router(controller_workflows.router)

from app.api.v1 import query_results
router.include_router(query_results.router)
