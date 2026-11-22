from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..auth import current_user, require_roles
from ..database import get_db
from . import service
from .schemas import SettlementCorrection, SettlementInput, SettlementReview

router = APIRouter(prefix='/plans/{plan_id}/pilot-settlements', tags=['试行场次结算'])


@router.get('')
def settlements(plan_id: int, db: Session = Depends(get_db), _user=Depends(current_user)):
    return service.board(db, plan_id)


@router.post('/{pilot_id}', status_code=201)
def record(plan_id: int, pilot_id: int, payload: SettlementInput, response: Response,
           db: Session = Depends(get_db), user=Depends(require_roles('boxoffice','admin'))):
    data, created = service.submit(db, plan_id, pilot_id, payload, user)
    db.commit()
    if not created:
        response.status_code = 200  # 同一回执重试，返回原单
    return data


@router.post('/{pilot_id}/corrections', status_code=201)
def correct(plan_id: int, pilot_id: int, payload: SettlementCorrection, response: Response,
            db: Session = Depends(get_db), user=Depends(require_roles('boxoffice','admin'))):
    data, created = service.correct(db, plan_id, pilot_id, payload, user)
    db.commit()
    if not created:
        response.status_code = 200
    return data


@router.post('/{pilot_id}/review')
def review(plan_id: int, pilot_id: int, payload: SettlementReview, db: Session = Depends(get_db),
           user=Depends(require_roles('revenue','admin'))):
    result = service.review(db, plan_id, pilot_id, payload, user)
    db.commit()
    return result
