import asyncio
import psycopg2
import httpx
import json
import logging
import subprocess
import time
import os
import sys
from datetime import datetime
from neo4j import GraphDatabase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import ONT

# Configuration — set via environment variables or .env file
_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_CONN = os.environ.get(
    "PG_CONN",
    f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "")
RETRIEVER_URL = "http://localhost:8070/v1/embeddings"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ConsolidationTest")

async def get_embedding(text):
    async with httpx.AsyncClient() as client:
        resp = await client.post(RETRIEVER_URL, json={"input": text, "model": "bge-m3"})
        return resp.json()["data"][0]["embedding"]

async def run_test():
    logger.info("Starting End-to-End Consolidation Test...")

    # 1. Start the daemon in the background with Mock LLM enabled
    env = os.environ.copy()
    env["MOCK_LLM"] = "1"
    daemon_proc = subprocess.Popen(["uv", "run", "--with", "httpx", "--with", "psycopg2-binary", "--with", "neo4j", "python", "shared-memory/scripts/consolidation_loop.py"], env=env)
    logger.info(f"Daemon started with PID {daemon_proc.pid} (MOCK_LLM=1)")

    try:
        # Give daemon time to start and LISTEN
        time.sleep(5)

        # 2. Insert 6 facts for a specific entity to trigger consolidation (threshold is 5)
        entity_name = f"TestEntity_{int(time.time())}"
        logger.info(f"Inserting 6 facts for entity: {entity_name}")

        conn = psycopg2.connect(PG_CONN)
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

        for i in range(6):
            content = f"Fact {i} about {entity_name}: This is a test fact for consolidation."
            embedding = await get_embedding(content)

            # Save to Postgres
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO technical_docs (content, embedding, metadata)
                    VALUES (%s, %s, %s) RETURNING id
                """, (content, embedding, json.dumps({"source": "test"})))
                pg_id = cur.fetchone()[0]

            # Mirror to Neo4j (Mocking the agent behavior)
            with driver.session() as session:
                session.run(
                    f"MERGE (e:{ONT.entity} {{name: $entity}})"
                    f" CREATE (f:{ONT.fact} {{pg_id: $pg_id, content: $content, consolidated: false}})"
                    f" CREATE (f)-[:{ONT.entity_link_alias}]->(e)",
                    entity=entity_name, pg_id=pg_id, content=content)

            logger.info(f"Inserted fact {i} (PG ID: {pg_id})")

        conn.commit()

        # 3. Wait for idle threshold (60s) + some buffer
        logger.info("Waiting 75 seconds for idle threshold and consolidation...")
        time.sleep(75)

        # 4. Verify consolidation
        success = True
        with conn.cursor() as cur:
            cur.execute("SELECT content, metadata FROM community_summaries WHERE metadata->>'entity' = %s", (entity_name,))
            summary = cur.fetchone()
            if summary:
                logger.info("SUCCESS: Found community summary in Postgres!")
                logger.info(f"Summary Content: {summary[0]}")
            else:
                logger.error("FAILURE: No community summary found in Postgres.")
                success = False

        with driver.session() as session:
            result = session.run(
                f"MATCH (f:{ONT.fact})-[:{ONT.entity_link_alias}]->(e:{ONT.entity} {{name: $entity}})"
                " RETURN count(f) as total, sum(case when f.consolidated = true then 1 else 0 end) as consolidated",
                entity=entity_name)
            record = result.single()
            if record["consolidated"] == 6:
                logger.info("SUCCESS: All facts in Neo4j marked as consolidated!")
            else:
                logger.error(f"FAILURE: Only {record['consolidated']}/6 facts marked as consolidated.")
                success = False

        driver.close()
        conn.close()

        if not success:
            exit(1)

    finally:
        logger.info("Terminating daemon...")
        daemon_proc.terminate()
        daemon_proc.wait()

if __name__ == "__main__":
    asyncio.run(run_test())
