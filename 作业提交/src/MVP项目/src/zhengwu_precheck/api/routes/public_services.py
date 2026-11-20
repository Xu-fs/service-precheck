"""只读便民指南页，公开展示已核对的官方办事资料。"""
from fastapi import APIRouter, Request

from zhengwu_precheck.core.services.public_services import GUIDES

router = APIRouter()


def guide_page(request, guide_id):
    return request.app.state.templates.TemplateResponse(
        request=request, name='public_service.html',
        context={'guide': GUIDES[guide_id], 'guides': GUIDES.values()},
    )


@router.get('/services/id-card-replacement')
async def identity_replacement(request: Request):
    return guide_page(request, 'id-card-replacement')


@router.get('/services/public-venues')
async def public_venues(request: Request):
    return guide_page(request, 'public-venues')
