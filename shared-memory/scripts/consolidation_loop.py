import sys
import os
import json
import gzip
import psycopg2
import psycopg2.extensions
import httpx
import asyncio
import logging
import select
from datetime import datetime
from neo4j import GraphDatabase

# Configuration — set via environment variables or .env file
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "")
_pg_pass = os.environ.get("PG_PASSWORD", "")
PG_CONN = os.environ.get(
    "PG_CONN",
    f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data"
)
RETRIEVER_URL = "http://localhost:8888/v1/embeddings"
REASONER_URL = "http://localhost:8888/v1/chat/completions"
IDLE_THRESHOLD_SEC = 60  # 1 minute for testing, change to 900 for 15 mins
MAX_DEFERRAL_SEC = IDLE_THRESHOLD_SEC * 3
DENSITY_THRESHOLD = 5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ConsolidationDaemon")

_LOG_TOOLS = ["memory_bridge", "vector_skill"]

def merge_logs(log_dir: str) -> None:
    """Logrotate pattern: rename per-tool logs, merge by timestamp, write shared_memory_YYYY-MM-DD.log.gz."""
    all_entries = []
    rotating_files = []

    for tool in _LOG_TOOLS:
        log_path = os.path.join(log_dir, f"{tool}.log")
        if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
            if os.path.exists(log_path):
                os.remove(log_path)  # clean up empty file
            continue
        rotating_path = log_path + ".rotating"
        try:
            os.rename(log_path, rotating_path)
        except OSError as e:
            logger.warning(f"merge_logs: could not rename {log_path}: {e}")
            continue
        rotating_files.append(rotating_path)
        with open(rotating_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    all_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(f"merge_logs: skipping malformed line in {rotating_path}: {line[:80]}")

    if not all_entries:
        for rp in rotating_files:
            try:
                os.remove(rp)
            except OSError:
                pass
        return

    # Group by calendar date (entries may span multiple days if daemon was down)
    by_date: dict = {}
    for entry in all_entries:
        try:
            entry_date = datetime.fromisoformat(entry["ts"]).date()
        except (KeyError, ValueError):
            entry_date = datetime.now().date()
        by_date.setdefault(entry_date, []).append(entry)

    for date, entries in by_date.items():
        out_path = os.path.join(log_dir, f"shared_memory_{date}.log.gz")
        tmp_path = out_path + ".tmp"

        # Merge with existing archive for this date if present
        existing: list = []
        if os.path.exists(out_path):
            try:
                with gzip.open(out_path, "rt", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                existing.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            except Exception as e:
                logger.warning(f"merge_logs: could not read existing archive {out_path}: {e}")

        merged = sorted(existing + entries, key=lambda e: e.get("ts", ""))
        try:
            with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
                for entry in merged:
                    f.write(json.dumps(entry) + "\n")
            os.replace(tmp_path, out_path)
            logger.info(f"merge_logs: {len(merged)} entries → {os.path.basename(out_path)}")
        except Exception as e:
            logger.error(f"merge_logs: failed writing {out_path}: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    for rp in rotating_files:
        try:
            os.remove(rp)
        except OSError:
            pass

class ConsolidationDaemon:
    def __init__(self):
        self.pending_pg_ids = set()
        self.last_activity = datetime.now()
        self.first_notification_time = None
        self.driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        self.is_running = True
        self.last_log_merge_date = None

    async def get_embedding(self, text):
        """Standardized 1024-dim BGE-M3 embedding call."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(RETRIEVER_URL, json={"input": text, "model": "bge-m3"})
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"Embedding error: {str(e)}")
            return None

    async def generate_summary(self, entity, facts, previous_summary=None):
        """Generate a cumulative narrative summary using the Hive-Mind Gateway."""
        if os.getenv("MOCK_LLM") == "1":
            return f"Mocked Summary for {entity}: Integrated {len(facts)} facts."

        if previous_summary:
            prompt = (
                f"You are maintaining a shared technical memory for '{entity}'.\n"
                f"Below is the EXISTING summary and a list of NEW facts.\n"
                f"Task: Integrate the NEW facts into a single, cohesive, updated narrative. "
                f"Maintain the technical depth and context of the original while expanding it.\n\n"
                f"### EXISTING SUMMARY:\n{previous_summary}\n\n"
                f"### NEW FACTS:\n" + "\n".join(f"- {f}" for f in facts) +
                f"\n\n### UPDATED NARRATIVE:"
            )
        else:
            prompt = (
                f"Summarize the following technical facts about '{entity}' into a cohesive, "
                f"concise narrative summary for a shared memory system. Focus on technical decisions "
                f"and outcomes.\n\nFacts:\n" + "\n".join(f"- {f}" for f in facts)
            )

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(REASONER_URL, json={
                    "model": "local-model",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1
                })
                if resp.status_code != 200:
                    logger.error(f"Summarization failed with status {resp.status_code}: {resp.text}")
                    return None
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Summarization error for {entity}: {type(e).__name__}: {str(e)}")
            return None

    async def run_consolidation_cycle(self):
        """Targeted density-based consolidation using pending_pg_ids as entry points."""
        if not self.pending_pg_ids:
            return

        logger.info(f"Sleep cycle triggered. Evaluating density for {len(self.pending_pg_ids)} entry points...")
        ids_to_process = list(self.pending_pg_ids)
        self.pending_pg_ids.clear()
        self.first_notification_time = None

        clusters = []
        try:
            with self.driver.session() as session:
                result = session.run("""
                    MATCH (f:Fact) WHERE f.pg_id IN $ids
                    MATCH (f)-[:REPORTS_ON|MENTIONS]->(e:Entity)
                    WITH DISTINCT e
                    MATCH (e)<-[:REPORTS_ON|MENTIONS]-(neighbor:Fact)
                    WHERE coalesce(neighbor.consolidated, false) = false
                    WITH e, collect(neighbor) as unflagged_facts
                    WHERE size(unflagged_facts) >= $threshold
                    RETURN e.name as entity,
                           [fact IN unflagged_facts | fact.content] as contents,
                           [fact IN unflagged_facts | fact.pg_id] as pg_ids
                """, ids=ids_to_process, threshold=DENSITY_THRESHOLD)
                clusters = result.data()

            if not clusters:
                logger.info("No high-density clusters found to consolidate.")
                return

            conn = psycopg2.connect(PG_CONN)
            try:
                for cluster in clusters:
                    entity = cluster['entity']
                    contents = cluster['contents']
                    pg_ids = cluster['pg_ids']

                    # 1. Fetch previous summary (Postgres)
                    previous_summary = None
                    try:
                        with conn.cursor() as cur:
                            cur.execute("""
                                SELECT content FROM community_summaries
                                WHERE metadata->>'entity' = %s
                                ORDER BY id DESC LIMIT 1
                            """, (entity,))
                            row = cur.fetchone()
                            if row:
                                previous_summary = row[0]
                    except Exception as e:
                        logger.warning(f"Failed to fetch previous summary for {entity}: {str(e)}")

                    # 2. Summarize (Long-running LLM call - No DB sessions held)
                    logger.info(f"Distilling cluster for '{entity}' ({len(contents)} facts)...")
                    summary = await self.generate_summary(entity, contents, previous_summary)
                    if not summary:
                        logger.error(f"Failed to generate summary for {entity}. Re-queueing IDs.")
                        self.pending_pg_ids.update(pg_ids)
                        continue

                    # 3. Vectorize
                    logger.info(f"Generated summary for '{entity}'. Vectorizing...")
                    embedding = await self.get_embedding(summary)
                    if not embedding:
                        logger.error(f"Failed to vectorize summary for {entity}. Re-queueing IDs.")
                        self.pending_pg_ids.update(pg_ids)
                        continue

                    # 4. Atomic Multi-DB Update
                    metadata = {
                        "type": "community_summary",
                        "entity": entity,
                        "source_pg_ids": pg_ids,
                        "timestamp": datetime.now().isoformat()
                    }

                    try:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO community_summaries (content, metadata, embedding)
                                VALUES (%s, %s, %s)
                                RETURNING id
                            """, (summary, json.dumps(metadata), embedding))
                            summary_pg_id = cur.fetchone()[0]

                        logger.info(f"Saved summary (ID: {summary_pg_id}) to Postgres. Syncing to Graph...")

                        # NOTE: CROSS-DB ATOMICITY RISK — see ADR.md
                        with self.driver.session() as session:
                            session.run("""
                                UNWIND $fact_ids as fid
                                MATCH (f:Fact {pg_id: fid})
                                SET f.consolidated = true
                                WITH collect(f) as facts
                                MERGE (s:CommunitySummary {pg_id: $summary_pg_id})
                                ON CREATE SET s.created_at = datetime()
                                SET s.entity = $entity,
                                    s.updated_at = datetime()
                                WITH s, facts
                                UNWIND facts as f
                                MERGE (f)-[:SUMMARIZED_BY]->(s)
                            """, fact_ids=pg_ids, summary_pg_id=summary_pg_id, entity=entity)

                        conn.commit()
                        logger.info(f"Successfully consolidated {len(pg_ids)} facts for '{entity}'.")
                    except Exception as e:
                        conn.rollback()
                        logger.error(f"Database write error for {entity}: {str(e)}")
                        self.pending_pg_ids.update(pg_ids)
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Consolidation cycle failed: {str(e)}")
            self.pending_pg_ids.update(ids_to_process)

    async def listen_for_events(self):
        """Asynchronous LISTEN on Postgres with non-blocking poll and hard backstop."""
        conn = psycopg2.connect(PG_CONN, application_name="consolidation_daemon")
        try:
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute("LISTEN new_artifact;")
            logger.info("Listening for 'new_artifact' notifications...")

            while self.is_running:
                if select.select([conn], [], [], 1.0) == ([], [], []):
                    now = datetime.now()

                    # Daily log merge — runs once per calendar day on first poll of the new day
                    today = now.date()
                    if self.last_log_merge_date != today:
                        log_dir = os.path.expanduser(os.environ.get("MEMORY_LOG_PATH", "~/.shared-memory/logs"))
                        if os.path.isdir(log_dir):
                            merge_logs(log_dir)
                        self.last_log_merge_date = today

                    seconds_since_activity = (now - self.last_activity).total_seconds()

                    should_consolidate = False
                    if self.pending_pg_ids:
                        if seconds_since_activity >= IDLE_THRESHOLD_SEC:
                            logger.info("Idle threshold reached. Starting consolidation.")
                            should_consolidate = True
                        elif self.first_notification_time:
                            seconds_since_first = (now - self.first_notification_time).total_seconds()
                            if seconds_since_first >= MAX_DEFERRAL_SEC:
                                logger.info(f"Hard backstop reached ({seconds_since_first:.1f}s). Forcing consolidation.")
                                should_consolidate = True

                    if should_consolidate:
                        await self.run_consolidation_cycle()
                else:
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        try:
                            payload = json.loads(notify.payload)
                            pg_id = payload.get("pg_id")
                            if pg_id:
                                logger.info(f"Received notification for pg_id: {pg_id}")
                                if not self.pending_pg_ids:
                                    self.first_notification_time = datetime.now()
                                self.pending_pg_ids.add(pg_id)
                                self.last_activity = datetime.now()
                        except json.JSONDecodeError:
                            logger.error(f"Failed to decode notification payload: {notify.payload}")
        finally:
            conn.close()
            logger.info("Postgres listener connection closed.")

    def stop(self):
        self.is_running = False
        self.driver.close()

async def main():
    daemon = ConsolidationDaemon()
    try:
        await daemon.listen_for_events()
    except KeyboardInterrupt:
        logger.info("Stopping daemon...")
        daemon.stop()

if __name__ == "__main__":
    asyncio.run(main())
