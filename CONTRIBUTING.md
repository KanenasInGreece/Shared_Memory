# Contributing to the Shared Memory Framework

Thank you for your interest in contributing. This document explains how to work with the project so your effort is not wasted and your PR has the best chance of being merged.

## Before You Write Code

For anything beyond a typo fix or documentation tweak, **open an issue first**. Describe what you want to change and why. This avoids the situation where you invest time in a PR that conflicts with the project's direction or something already in progress.

For bug reports, include:
- What you did
- What you expected
- What actually happened
- Relevant log output (redact any credentials)

## The Contribution Workflow

1. **Fork** the repository on GitHub.
2. **Clone** your fork locally.
3. Create a **feature branch** — do not work directly on `main`.
   ```bash
   git checkout -b fix/daemon-notify-timeout
   ```
4. Make your changes, run the tests, then push to your fork.
5. Open a **Pull Request** against `main` in this repository.
6. Respond to review feedback. PRs are merged by the repository owner after review.

Direct push access is not granted. All changes enter through PRs.

## Key Invariants — Do Not Break These

These constraints are architectural. A PR that violates them will not be merged regardless of how clean the code is.

- **All embedding and reranking calls must route through the Hive-Mind Gateway on port 8888.** Never call port 8070 (BGE-M3) or 8071 (BGE-Reranker-v2-m3) directly. The 1024-dim consistency guarantee depends on this.
- **Saves must abort if the gateway is unreachable.** An artifact stored without a vector is permanently invisible to semantic search. This failure must surface, never be swallowed.
- **`pg_notify` must fire inside the same transaction as the INSERT**, before `conn.commit()`. If the notification is decoupled from the commit, it can be permanently lost.
- **Facts saved without `"entities"` in metadata are never eligible for Tier 3 consolidation.** This is intentional — do not add a workaround that bypasses the entity requirement.
- **SHA-256 idempotency** — `ON CONFLICT (content_hash) DO UPDATE` must remain. Re-saving identical content must be safe.

## No Hardcoded Credentials

Every value that varies between deployments — passwords, API keys, connection strings, file paths — must be read from environment variables using `os.environ.get(...)`. Use `YOUR_*` as the placeholder name in config files.

A PR that introduces a hardcoded credential will be rejected immediately, regardless of what else it contains.

## Running the Tests

All tests are fully mocked — no live database or gateway is required.

```bash
# Full suite
uv run --with pytest --with pytest-asyncio --with fastmcp \
       --with psycopg2-binary --with httpx --with neo4j \
       pytest tests/ -v

# Skip LLM calls in consolidation tests
MOCK_LLM=1 uv run --with pytest --with pytest-asyncio --with fastmcp \
           --with psycopg2-binary --with httpx --with neo4j \
           pytest tests/test_consolidation_e2e.py
```

Run the full suite before opening a PR. A PR that breaks existing tests will not be reviewed until the tests pass.

## Code Style

- No hardcoded values (see above).
- No comments that describe *what* the code does — only *why*, and only when the reason is non-obvious.
- Match the style of the file you are editing. There is no linter enforced; use judgment.
- Keep changes focused. A PR that fixes a bug and also refactors unrelated code is harder to review and easier to reject.

## AI Tools

You are welcome to use AI coding assistants (Claude Code, Copilot, Cursor, or similar) when working on contributions. They are tools, not authors. Do not add `Co-Authored-By` AI attribution lines to commit messages — the human submitting the PR is the author and is responsible for every line in it.

## Licensing

By submitting a Pull Request you agree that your contribution will be licensed under the [Apache License, Version 2.0](LICENSE), the same licence as this project.

Do not introduce dependencies with licences incompatible with Apache 2.0 (e.g. GPL) without raising it in the issue first.
