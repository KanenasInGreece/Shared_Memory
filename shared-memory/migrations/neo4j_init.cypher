// neo4j_init.cypher — Neo4j schema for the Shared Memory Framework
//
// These constraints are now applied AUTOMATICALLY on every gateway start
// (coordinator._ensure_neo4j_schema). Run this file manually only to
// reinitialise a Neo4j instance before the gateway is started for the
// first time, or to verify the schema on an existing instance.
//
// Usage (cypher-shell):
//   cypher-shell -u neo4j -p <password> < shared-memory/migrations/neo4j_init.cypher
// Usage (Neo4j Browser):
//   Paste and run each statement individually.
//
// All statements are idempotent (IF NOT EXISTS). Safe to re-run.

// ── Core nodes ────────────────────────────────────────────────────────────────

// Fact: one per technical_docs row; pg_id is the Postgres row id
CREATE CONSTRAINT fact_pg_id IF NOT EXISTS
    FOR (n:Fact) REQUIRE n.pg_id IS UNIQUE;

// Entity: named concept that anchors consolidation clusters
CREATE CONSTRAINT entity_name IF NOT EXISTS
    FOR (n:Entity) REQUIRE n.name IS UNIQUE;

// CommunitySummary: one per community_summaries row
CREATE CONSTRAINT community_summary_pg_id IF NOT EXISTS
    FOR (n:CommunitySummary) REQUIRE n.pg_id IS UNIQUE;

// ── Provenance nodes (PROV-O layer) ───────────────────────────────────────────

// Decision: one per technical_docs row of type=decision
CREATE CONSTRAINT decision_pg_id IF NOT EXISTS
    FOR (n:Decision) REQUIRE n.pg_id IS UNIQUE;

// Human: the person who owns a decision (decided_by field)
CREATE CONSTRAINT human_name IF NOT EXISTS
    FOR (n:Human) REQUIRE n.name IS UNIQUE;

// AIAgent: the AI tool that assisted (assisted_by field)
CREATE CONSTRAINT ai_agent_name IF NOT EXISTS
    FOR (n:AIAgent) REQUIRE n.name IS UNIQUE;

// Project: project scope node
CREATE CONSTRAINT project_name IF NOT EXISTS
    FOR (n:Project) REQUIRE n.name IS UNIQUE;
