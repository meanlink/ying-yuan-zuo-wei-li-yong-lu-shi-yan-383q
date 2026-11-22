"""已有 PostgreSQL 卷的幂等迁移：把单版结算表升级为版本链。

新建库由 Base.metadata.create_all 直接建出新结构；老库在这里补齐列、
替换旧约束。旧库每个试行场次至多一条结算，默认 version=1、is_current=TRUE
即可保持语义不变。所有语句均可重复执行。
"""
from sqlalchemy import text

STATEMENTS = [
    "ALTER TABLE pilot_settlements ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE pilot_settlements ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE pilot_settlements ADD COLUMN IF NOT EXISTS diff_sources TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE pilot_settlements ADD COLUMN IF NOT EXISTS superseded_by INTEGER",
    # 旧版约束：每场次一条、售出不超过投放、三态枚举、自动命名的凭证唯一约束，全部按新版替换
    "ALTER TABLE pilot_settlements DROP CONSTRAINT IF EXISTS pilot_settlements_pilot_id_key",
    "ALTER TABLE pilot_settlements DROP CONSTRAINT IF EXISTS pilot_settlements_tickets_sold_check",
    "ALTER TABLE pilot_settlements DROP CONSTRAINT IF EXISTS pilot_settlements_state_check",
    "ALTER TABLE pilot_settlements DROP CONSTRAINT IF EXISTS pilot_settlements_pos_reference_key",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_settlement_pilot_version') THEN
            ALTER TABLE pilot_settlements ADD CONSTRAINT uq_settlement_pilot_version UNIQUE (pilot_id, version);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_settlement_pos_reference') THEN
            ALTER TABLE pilot_settlements ADD CONSTRAINT uq_settlement_pos_reference UNIQUE (pos_reference);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_settlement_state') THEN
            ALTER TABLE pilot_settlements ADD CONSTRAINT ck_settlement_state
                CHECK (state IN ('submitted','held','approved','rejected','superseded','recheck'));
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_settlement_tickets_sold') THEN
            ALTER TABLE pilot_settlements ADD CONSTRAINT ck_settlement_tickets_sold CHECK (tickets_sold >= 0);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_settlement_seats_offered') THEN
            ALTER TABLE pilot_settlements ADD CONSTRAINT ck_settlement_seats_offered CHECK (seats_offered > 0);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_settlement_superseded_by') THEN
            ALTER TABLE pilot_settlements ADD CONSTRAINT fk_settlement_superseded_by
                FOREIGN KEY (superseded_by) REFERENCES pilot_settlements (id);
        END IF;
    END $$;
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_settlement_current ON pilot_settlements (pilot_id) WHERE is_current",
]


def run_migrations(conn):
    if conn.dialect.name != 'postgresql':
        return
    for statement in STATEMENTS:
        conn.execute(text(statement))
