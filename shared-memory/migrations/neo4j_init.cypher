// neo4j_init.cypher — Neo4j schema for the Shared Memory Framework
//
// Run this ONCE on a fresh Neo4j instance, before the first gateway start.
// Neo4j constraints are NOT created automatically — without them, MERGE races
// can create duplicate Entity / Fact / Decision nodes. This is the Neo4j
// counterpart to schema_init.sql for Postgres.
//
// Usage (cypher-shell):
//   cypher-shell -u neo4j -p <password> < shared-memory/migrations/neo4j_init.cypher
// Usage (Neo4j Browser):
//   Paste and run each statement individually.
//
// All statements are idempotent (IF NOT EXISTS) — safe to re-run on an
// existing instance to verify or repair the constraint set.

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

// Project identity: the registry id (migration 027). The name above is a LABEL
// and may be renamed; this is the key the axis edges hang off and the key the
// insight gate counts distinct projects by, so two nodes claiming one identity
// would let a single project pass a cross-project rule. Nodes that do not carry
// the property yet are unaffected — a deployment mid-upgrade still writes, and
// reconcile_project_identity.py is what completes it.
CREATE CONSTRAINT project_identity IF NOT EXISTS
    FOR (n:Project) REQUIRE n.project_id IS UNIQUE;
