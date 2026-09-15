"""Evidence Fabric v1: persistent Topics, Evidence, lineage and task refs.

Revision ID: 0003_evidence_fabric_v1
Revises: 0002_asset_query_links
Create Date: 2026-09-12
"""
from __future__ import annotations

from alembic import op

revision = "0003_evidence_fabric_v1"
down_revision = "0002_asset_query_links"
branch_labels = None
depends_on = None


def _policy(table: str, predicate: str) -> None:
    name = f"tenant_isolation_{table}"
    expr = f"conversation_security.bypass_rls() OR ({predicate})"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
    op.execute(f"CREATE POLICY {name} ON {table} FOR ALL USING ({expr}) WITH CHECK ({expr})")


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS conversation_topics (
        topic_id uuid PRIMARY KEY,
        conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        title text NOT NULL,
        topic_summary text NOT NULL DEFAULT '',
        primary_subject jsonb NOT NULL DEFAULT '{}'::jsonb,
        scope jsonb NOT NULL DEFAULT '{}'::jsonb,
        current_goal text,
        status varchar(16) NOT NULL DEFAULT 'ACTIVE',
        searchable_text text NOT NULL DEFAULT '',
        last_task_id uuid REFERENCES generation_tasks(id) ON DELETE SET NULL,
        version integer NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_conversation_topics_conversation ON conversation_topics(conversation_id, updated_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_conversation_topics_searchable ON conversation_topics(conversation_id, status)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS conversation_context_slots (
        conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        slot_no smallint NOT NULL,
        topic_id uuid NOT NULL REFERENCES conversation_topics(topic_id) ON DELETE CASCADE,
        activated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(conversation_id, slot_no),
        CONSTRAINT ck_context_slot_hot_window CHECK(slot_no IN (0,1,2))
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_context_slots_topic ON conversation_context_slots(topic_id)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS evidence_objects (
        evidence_id uuid PRIMARY KEY,
        conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        kind varchar(32) NOT NULL,
        semantic_type varchar(96) NOT NULL,
        authority varchar(32) NOT NULL,
        subject jsonb NOT NULL DEFAULT '{}'::jsonb,
        scope jsonb NOT NULL DEFAULT '{}'::jsonb,
        content_descriptor jsonb NOT NULL DEFAULT '{}'::jsonb,
        summary jsonb NOT NULL DEFAULT '{}'::jsonb,
        completeness jsonb NOT NULL DEFAULT '{}'::jsonb,
        freshness jsonb NOT NULL DEFAULT '{}'::jsonb,
        storage_backend varchar(48) NOT NULL,
        storage_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
        inline_payload jsonb,
        source_system varchar(96) NOT NULL,
        source_task_id uuid REFERENCES generation_tasks(id) ON DELETE SET NULL,
        source_tool varchar(192),
        immutable boolean NOT NULL DEFAULT true,
        supersedes_evidence_id uuid REFERENCES evidence_objects(evidence_id) ON DELETE SET NULL,
        checksum varchar(128),
        observed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_evidence_conversation ON evidence_objects(conversation_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_evidence_semantic_type ON evidence_objects(semantic_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_evidence_source_task ON evidence_objects(source_task_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_evidence_created_at ON evidence_objects(created_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS topic_evidence_refs (
        topic_id uuid NOT NULL REFERENCES conversation_topics(topic_id) ON DELETE CASCADE,
        evidence_id uuid NOT NULL REFERENCES evidence_objects(evidence_id) ON DELETE CASCADE,
        role varchar(24) NOT NULL DEFAULT 'supporting',
        pinned boolean NOT NULL DEFAULT false,
        added_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(topic_id,evidence_id)
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS evidence_lineage_edges (
        parent_evidence_id uuid NOT NULL REFERENCES evidence_objects(evidence_id) ON DELETE CASCADE,
        child_evidence_id uuid NOT NULL REFERENCES evidence_objects(evidence_id) ON DELETE CASCADE,
        relation_type varchar(48) NOT NULL,
        metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(parent_evidence_id,child_evidence_id,relation_type)
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS task_evidence_refs (
        task_id uuid NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
        evidence_id uuid NOT NULL REFERENCES evidence_objects(evidence_id) ON DELETE CASCADE,
        direction varchar(8) NOT NULL,
        purpose varchar(128),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(task_id,evidence_id,direction),
        CONSTRAINT ck_task_evidence_direction CHECK(direction IN ('INPUT','OUTPUT'))
    )""")

    owner = "EXISTS (SELECT 1 FROM conversations c WHERE c.id = {table}.conversation_id AND c.user_token = conversation_security.current_user_token())"
    _policy("conversation_topics", owner.format(table="conversation_topics"))
    _policy("conversation_context_slots", owner.format(table="conversation_context_slots"))
    _policy("evidence_objects", owner.format(table="evidence_objects"))
    _policy("topic_evidence_refs", "EXISTS (SELECT 1 FROM conversation_topics t JOIN conversations c ON c.id=t.conversation_id WHERE t.topic_id=topic_evidence_refs.topic_id AND c.user_token=conversation_security.current_user_token())")
    _policy("evidence_lineage_edges", "EXISTS (SELECT 1 FROM evidence_objects e JOIN conversations c ON c.id=e.conversation_id WHERE e.evidence_id=evidence_lineage_edges.child_evidence_id AND c.user_token=conversation_security.current_user_token())")
    _policy("task_evidence_refs", "EXISTS (SELECT 1 FROM generation_tasks t JOIN conversations c ON c.id=t.conversation_id WHERE t.id=task_evidence_refs.task_id AND c.user_token=conversation_security.current_user_token())")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_evidence_refs")
    op.execute("DROP TABLE IF EXISTS evidence_lineage_edges")
    op.execute("DROP TABLE IF EXISTS topic_evidence_refs")
    op.execute("DROP TABLE IF EXISTS evidence_objects")
    op.execute("DROP TABLE IF EXISTS conversation_context_slots")
    op.execute("DROP TABLE IF EXISTS conversation_topics")
