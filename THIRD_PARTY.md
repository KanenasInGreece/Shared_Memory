# Third-party components

Every runtime component the Shared Memory Framework couples to, its licence, and *how* the framework
couples to it — maintained at every dependency-currency check (policy in [`SECURITY.md`](SECURITY.md)).
The framework itself is licensed under Apache-2.0 ([`LICENSE`](LICENSE)). Nothing below is redistributed
by this repository: images are pulled by Docker Compose from their publishers, models are downloaded from
Hugging Face, and Python packages are resolved by `uv` at run time.

Last checked: **2026-08-25** (v0.9.55). Licences were read from each project's licence file or registry
metadata at that date, not recalled.

| Component | Version pinned / used | Licence | Coupling |
|---|---|---|---|
| PostgreSQL | 17.11 | PostgreSQL Licence | separate process (TCP) |
| pgvector | 0.8.6 | PostgreSQL Licence | extension inside PostgreSQL |
| Neo4j Community Edition | 5.26.30 (LTS) | **GPLv3** | separate process, reached over Bolt through the Apache-2.0 driver; image pulled by Compose |
| APOC | loaded by the Neo4j container at start | Apache-2.0 | plugin inside Neo4j |
| Neo4j Graph Data Science (community) | loaded by the Neo4j container at start | **GPLv3** | plugin inside Neo4j, called over Bolt; fetched by the container from Neo4j's servers |
| llama.cpp server (`server`, `server-vulkan` images) | floating tags | MIT | separate processes (HTTP) |
| BGE-M3 (embedder model) | Q8_0 GGUF | MIT | model file loaded by llama.cpp |
| bge-reranker-v2-m3 (reranker model) | Q8_0 GGUF | Apache-2.0 | model file loaded by llama.cpp |
| `neo4j` Python driver | resolved by `uv` | Apache-2.0 AND Python-2.0 | imported library |
| `asyncpg`, `fastmcp` | resolved by `uv` | Apache-2.0 | imported libraries |
| `aiohttp` | resolved by `uv` | Apache-2.0 AND MIT | imported library |
| `httpx`, `python-dotenv` | resolved by `uv` | BSD-3-Clause | imported libraries |
| `numpy` | resolved by `uv` | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | imported library |
| `json-repair` | resolved by `uv` | MIT | imported library |
| `psycopg2-binary` | resolved by `uv` | LGPL with exceptions (PyPI metadata) | imported library, used unmodified and dynamically, within its linking exception |

## The copyleft components, stated plainly

Neo4j Community Edition and Graph Data Science are GPLv3. This framework does not bundle, link
against, or extend them: they run as their own process, the framework talks to them over the network
through Neo4j's own Apache-2.0 driver, and the images and plugin jars are obtained from Neo4j, not from
this repository. Under the widely held reading that network use of a GPL program does not create a
derivative work, the framework's Apache-2.0 licence is unaffected. This is the position the project
takes; it is not legal advice.

What would change it, and is therefore not done: shipping Neo4j or GDS artefacts in this repository or
in a release; writing a Neo4j procedure or plugin (it would itself have to be GPL); or a licence change
on the Community line, which the currency check watches for.
