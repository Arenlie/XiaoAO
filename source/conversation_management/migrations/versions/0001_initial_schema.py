"""Conversation Management Backend 1.0.0 initial schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-05
"""
from __future__ import annotations

from alembic import op

from app.models import all_models  # noqa: F401
from app.models.base import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

_DIRECT_POLICIES = {
    "conversations": "user_token = conversation_security.current_user_token()",
    "user_profiles": "user_token = conversation_security.current_user_token()",
    "query_statistics": "user_token = conversation_security.current_user_token()",
    "attachments": "user_token = conversation_security.current_user_token()",
    "attachment_content_manifests": "user_token = conversation_security.current_user_token()",
    "attachment_content_artifacts": "user_token = conversation_security.current_user_token()",
    "attachment_content_chunks": "user_token = conversation_security.current_user_token()",
}

_CONVERSATION_CHILDREN = (
    "conversation_branches",
    "messages",
    "generation_tasks",
    "pending_entity_selections",
    "context_snapshots",
    "outbox_events",
)


def _expression(predicate: str) -> str:
    return f"conversation_security.bypass_rls() OR ({predicate})"


def _apply_policy(table: str, predicate: str) -> None:
    policy = f"tenant_isolation_{table}"
    expression = _expression(predicate)
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute(
        f"CREATE POLICY {policy} ON {table} FOR ALL "
        f"USING ({expression}) WITH CHECK ({expression})"
    )


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())
    op.execute("CREATE SCHEMA IF NOT EXISTS conversation_security")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION conversation_security.current_user_token()
        RETURNS text LANGUAGE sql STABLE AS $$
          SELECT COALESCE(current_setting('app.user_token', true), '')
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION conversation_security.bypass_rls()
        RETURNS boolean LANGUAGE sql STABLE AS $$
          SELECT COALESCE(current_setting('app.rls_bypass', true), 'off') = 'on'
        $$
        """
    )
    for table, predicate in _DIRECT_POLICIES.items():
        _apply_policy(table, predicate)
    for table in _CONVERSATION_CHILDREN:
        _apply_policy(
            table,
            f"EXISTS (SELECT 1 FROM conversations c "
            f"WHERE c.id = {table}.conversation_id "
            f"AND c.user_token = conversation_security.current_user_token())",
        )
    _apply_policy(
        "task_execution_events",
        "EXISTS (SELECT 1 FROM generation_tasks t "
        "JOIN conversations c ON c.id = t.conversation_id "
        "WHERE t.id = task_execution_events.task_id "
        "AND c.user_token = conversation_security.current_user_token())",
    )


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
    op.execute("DROP SCHEMA IF EXISTS conversation_security CASCADE")
