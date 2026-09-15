"""Private collection links, bound to actual messages and RLS conversation ownership."""
from alembic import op

revision = "0002_asset_query_links"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS asset_query_links (
        id uuid PRIMARY KEY, query_id uuid NOT NULL,
        conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        branch_id uuid NOT NULL REFERENCES conversation_branches(id) ON DELETE CASCADE,
        task_id uuid NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
        message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        subject text NOT NULL, payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(message_id,query_id))""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_asset_query_links_path ON asset_query_links(conversation_id,message_id)")
    op.execute("ALTER TABLE asset_query_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE asset_query_links FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_asset_query_links ON asset_query_links")
    op.execute("""CREATE POLICY tenant_isolation_asset_query_links ON asset_query_links FOR ALL
        USING (conversation_security.bypass_rls() OR EXISTS(SELECT 1 FROM conversations c
        WHERE c.id=conversation_id AND c.user_token=conversation_security.current_user_token()))
        WITH CHECK (conversation_security.bypass_rls() OR EXISTS(SELECT 1 FROM conversations c
        WHERE c.id=conversation_id AND c.user_token=conversation_security.current_user_token()))""")


def downgrade():
    op.execute("DROP TABLE IF EXISTS asset_query_links")
