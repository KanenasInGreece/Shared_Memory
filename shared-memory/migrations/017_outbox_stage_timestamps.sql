-- Migration 017: rem_reviewed_at and consolidated_at so stage latency is visible
-- while the outbox row still exists. The historical split is copied to
-- consolidation_runs.extra before the delete; coarse fact-to-summary time is
-- summary.created_at minus the source facts' created_at.

ALTER TABLE neo4j_outbox ADD COLUMN IF NOT EXISTS rem_reviewed_at TIMESTAMPTZ;
ALTER TABLE neo4j_outbox ADD COLUMN IF NOT EXISTS consolidated_at TIMESTAMPTZ;
