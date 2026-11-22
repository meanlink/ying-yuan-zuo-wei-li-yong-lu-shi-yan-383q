import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from ..models import AuditLog, RevenueReview, TicketObservation, now_utc
from ..operations_models import SchedulingPilot
from ..services.operations import plan_by_id
from .economics import calculate
from .models import PilotSettlement

LOCAL = ZoneInfo('Asia/Shanghai')

# 三方对账来源：排片回执实际投放、实验单元观测售出、POS 售出
DIFF_LABELS = {'schedule': '排片回执实际投放', 'observation': '实验单元观测售出'}


def eligible(pilot):
    show_start = datetime.combine(pilot.show_date, time.fromisoformat(pilot.show_time), tzinfo=LOCAL)
    return pilot.status == 'scheduled' and show_start < datetime.now(LOCAL)


def pilots(db, plan_id):
    plan_by_id(db, plan_id)
    return select(SchedulingPilot).join(RevenueReview).where(RevenueReview.plan_id == plan_id)


def unit_sales(db, unit_ids):
    """实验单元观测口径：观测座位数与售出数。"""
    sales = {unit_id: {'observed': 0, 'sold': 0} for unit_id in unit_ids}
    rows = db.execute(select(
        TicketObservation.unit_id, func.count(TicketObservation.id),
        func.coalesce(func.sum(case((TicketObservation.sold, 1), else_=0)), 0),
    ).where(TicketObservation.unit_id.in_(unit_ids)).group_by(TicketObservation.unit_id))
    for unit_id, observed, sold in rows:
        sales[unit_id] = {'observed': observed, 'sold': int(sold)}
    return sales


def misaligned(tickets_sold, pilot, observed_sold):
    """按实验单元对齐来源：POS 售出超过排片回执实际投放或实验单元观测售出时返回差异来源。"""
    sources = []
    if tickets_sold > pilot.actual_seats:
        sources.append('schedule')
    if tickets_sold > observed_sold:
        sources.append('observation')
    return sources


def row_json(row):
    data = {col.name: getattr(row, col.name) for col in PilotSettlement.__table__.columns}
    data['diff_sources'] = json.loads(row.diff_sources or '[]')
    data['utilization'] = row.tickets_sold / row.seats_offered if row.seats_offered else None
    return data


def board(db, plan_id):
    plan_by_id(db, plan_id)
    pilot_rows = [pilot for pilot in db.scalars(pilots(db, plan_id).order_by(
        SchedulingPilot.show_date.desc(), SchedulingPilot.id.desc())).all() if pilot.status == 'scheduled']
    if not pilot_rows:
        return {'items': []}
    versions = db.scalars(select(PilotSettlement).where(
        PilotSettlement.pilot_id.in_([pilot.id for pilot in pilot_rows]),
    ).order_by(PilotSettlement.version.desc())).all()
    by_pilot = {}
    for row in versions:
        by_pilot.setdefault(row.pilot_id, []).append(row)
    sales = unit_sales(db, [pilot.unit_id for pilot in pilot_rows])
    items = []
    for pilot in pilot_rows:
        history = by_pilot.get(pilot.id, [])
        current = next((row for row in history if row.is_current), None)
        observed = sales[pilot.unit_id]
        items.append({
            'pilot_id': pilot.id, 'receipt': pilot.booking_ref, 'hall': pilot.hall_name,
            'show_date': pilot.show_date, 'show_time': pilot.show_time, 'seats': pilot.actual_seats,
            'unit_id': pilot.unit_id, 'observed': observed,
            'can_submit': eligible(pilot) and current is None,
            'can_correct': current is not None,
            'settlement': row_json(current) if current else None,
            'alignment': misaligned(current.tickets_sold, pilot, observed['sold']) if current else [],
            'history': [row_json(row) for row in history],
        })
    return {'items': items}


def lock_pilot(db, plan_id, pilot_id):
    pilot = db.scalar(pilots(db, plan_id).where(SchedulingPilot.id == pilot_id).with_for_update(of=SchedulingPilot))
    if not pilot:
        raise HTTPException(404, '该排片试行不存在')
    return pilot


def current_settlement(db, pilot_id, *, lock=False):
    statement = select(PilotSettlement).where(PilotSettlement.pilot_id == pilot_id, PilotSettlement.is_current)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def replay_by_reference(db, pilot, payload):
    """同一 POS 回执重试不重复：内容一致返回原单，串场或内容不一致则冲突。"""
    existing = db.scalar(select(PilotSettlement).where(PilotSettlement.pos_reference == payload.pos_reference))
    if existing is None:
        return None
    if existing.pilot_id != pilot.id:
        raise HTTPException(409, '该 POS 凭证已用于其他试行场次')
    same = all(getattr(existing, field) == getattr(payload, field) for field in (
        'tickets_sold', 'gross_tickets_cents', 'ticket_commission_cents',
        'concession_margin_cents', 'incremental_cost_cents', 'subsidy_cents'))
    if not same:
        raise HTTPException(409, '该 POS 凭证已登记，且与本次提交内容不一致')
    return existing


def economics_or_422(payload):
    try:
        return calculate(payload.tickets_sold, payload.gross_tickets_cents, payload.ticket_commission_cents,
                         payload.concession_margin_cents, payload.incremental_cost_cents, payload.subsidy_cents)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def submit(db, plan_id, pilot_id, payload, user):
    pilot = lock_pilot(db, plan_id, pilot_id)
    replay = replay_by_reference(db, pilot, payload)
    if replay:
        return row_json(replay), False
    if not eligible(pilot):
        raise HTTPException(409, '待排片、已撤销或尚未开场的试行目前无法登记结算')
    if db.scalar(select(PilotSettlement.id).where(PilotSettlement.pilot_id == pilot.id)):
        raise HTTPException(409, '该试行场次已登记结算，请通过补正更新')
    result = economics_or_422(payload)
    observed = unit_sales(db, [pilot.unit_id])[pilot.unit_id]
    sources = misaligned(payload.tickets_sold, pilot, observed['sold'])
    row = PilotSettlement(pilot_id=pilot.id, version=1, is_current=True, seats_offered=pilot.actual_seats,
                          **payload.model_dump(), contribution_cents=result.contribution_cents,
                          breakeven_tickets=result.breakeven_tickets, diff_sources=json.dumps(sources),
                          state='held' if sources else 'submitted', submitted_by=user.id)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        raise HTTPException(409, '场次或 POS 凭证已登记') from error
    db.add(AuditLog(actor_id=user.id, action='pilot_settlement_submitted', entity_type='pilot_settlement',
                    entity_id=str(row.id), detail=f'{payload.pos_reference} · {row.state}'))
    return row_json(row), True


def correct(db, plan_id, pilot_id, payload, user):
    """补正：校验失败或仍不对齐时旧版原样保留；成功时同事务切换当前版本，利用率一次性生效。"""
    pilot = lock_pilot(db, plan_id, pilot_id)
    replay = replay_by_reference(db, pilot, payload)
    if replay:
        return row_json(replay), False
    current = current_settlement(db, pilot.id, lock=True)
    if not current:
        raise HTTPException(409, '该试行场次尚未登记结算，无法补正')
    if payload.expected_settlement_id != current.id:
        raise HTTPException(409, '结算已有新版本或复核结果，请刷新后再补正')
    result = economics_or_422(payload)
    observed = unit_sales(db, [pilot.unit_id])[pilot.unit_id]
    sources = misaligned(payload.tickets_sold, pilot, observed['sold'])
    if sources:
        labels = '、'.join(DIFF_LABELS[source] for source in sources)
        raise HTTPException(422, f'补正后售出数仍超过{labels}，旧版结算保留')
    # 先摘下旧版当前标记，再插入新版本，部分唯一索引保证任意时刻只有一个当前版本
    current.is_current = False
    current.state = 'recheck' if current.state == 'approved' else 'superseded'
    db.flush()
    row = PilotSettlement(pilot_id=pilot.id, version=current.version + 1, is_current=True,
                          seats_offered=pilot.actual_seats,
                          **payload.model_dump(exclude={'expected_settlement_id'}),
                          contribution_cents=result.contribution_cents,
                          breakeven_tickets=result.breakeven_tickets, diff_sources='[]',
                          state='submitted', submitted_by=user.id)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        raise HTTPException(409, '场次或 POS 凭证已登记') from error
    current.superseded_by = row.id
    db.add(AuditLog(actor_id=user.id, action='pilot_settlement_corrected', entity_type='pilot_settlement',
                    entity_id=str(row.id),
                    detail=f'{payload.pos_reference} · v{current.version}→v{row.version} · 旧版={current.state}'))
    return row_json(row), True


def review(db, plan_id, pilot_id, payload, user):
    pilot = lock_pilot(db, plan_id, pilot_id)
    row = current_settlement(db, pilot.id, lock=True)
    if not row:
        raise HTTPException(409, '该试行场次尚未登记结算')
    if row.id != payload.expected_settlement_id:
        raise HTTPException(409, '结算已被补正，请刷新后再复核')
    if row.state == 'held':
        raise HTTPException(409, '三方对账未对齐，暂缓复核，请先由票房补正')
    if row.state != 'submitted':
        raise HTTPException(409, '只有待复核结算单可以裁定')
    if row.submitted_by == user.id:
        raise HTTPException(409, '票务登记和收益复核须由不同人员完成')
    row.state = payload.decision
    row.review_note = payload.note
    row.reviewed_by = user.id
    row.reviewed_at = now_utc()
    db.add(AuditLog(actor_id=user.id, action='pilot_settlement_reviewed', entity_type='pilot_settlement',
                    entity_id=str(row.id), detail=payload.decision))
    return row_json(row)
