from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import now_utc


class PilotSettlement(Base):
    """试行场次结算的版本链。同一试行场次只保留一个 is_current 版本，
    补正插入新版本并一次性切换，旧版本整行保留，永不原地覆盖。
    状态：submitted 待复核 / held 暂缓复核（三方未对齐）/ approved 已确认 /
    rejected 已驳回 / superseded 已被补正取代 / recheck 已确认旧版待重核。"""
    __tablename__ = 'pilot_settlements'
    __table_args__ = (
        UniqueConstraint('pilot_id', 'version', name='uq_settlement_pilot_version'),
        UniqueConstraint('pos_reference', name='uq_settlement_pos_reference'),
        CheckConstraint("state IN ('submitted','held','approved','rejected','superseded','recheck')", name='ck_settlement_state'),
        CheckConstraint('tickets_sold >= 0', name='ck_settlement_tickets_sold'),
        CheckConstraint('seats_offered > 0', name='ck_settlement_seats_offered'),
        Index('uq_settlement_current', 'pilot_id', unique=True,
              postgresql_where=text('is_current'), sqlite_where=text('is_current')),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    pilot_id: Mapped[int] = mapped_column(ForeignKey('scheduling_pilots.id'), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    pos_reference: Mapped[str] = mapped_column(String(80))
    seats_offered: Mapped[int] = mapped_column(Integer)
    tickets_sold: Mapped[int] = mapped_column(Integer)
    gross_tickets_cents: Mapped[int] = mapped_column(BigInteger)
    ticket_commission_cents: Mapped[int] = mapped_column(BigInteger)
    concession_margin_cents: Mapped[int] = mapped_column(BigInteger)
    incremental_cost_cents: Mapped[int] = mapped_column(BigInteger)
    subsidy_cents: Mapped[int] = mapped_column(BigInteger)
    contribution_cents: Mapped[int] = mapped_column(BigInteger)
    breakeven_tickets: Mapped[int | None] = mapped_column(Integer)
    # 登记时三方对账差异来源（JSON 数组）：schedule=排片回执实际投放，observation=实验单元观测
    diff_sources: Mapped[str] = mapped_column(Text, default='[]')
    notes: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default='submitted')
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey('pilot_settlements.id', name='fk_settlement_superseded_by'))
    submitted_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
