# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately by emailing: **xsmotsenigos@googlemail.com**

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix if you have one

You will receive a response within 72 hours. Once the issue is confirmed and a fix is available, a public disclosure will be coordinated with you.

## Known Security Considerations

### Gateway binds to all interfaces (`0.0.0.0`)

`hive_mind_proxy.py` listens on `0.0.0.0:8888` by default, which means **any machine on your local network** can reach the embedding, reranking, and LLM proxy endpoints — no authentication required.

This is intentional for workstation use where the LAN is trusted, but you should be aware of it:

- On an untrusted network (public Wi-Fi, shared office), restrict the bind address to `127.0.0.1` by editing line 245 of `hive_mind_proxy.py`:
  ```python
  site = web.TCPSite(runner, "127.0.0.1", PORT)
  ```
- Alternatively, firewall port 8888 at the OS level to limit access to localhost only.

### No authentication on MCP endpoints

The MCP server (`vector-skill.py`) and the CLI bridge (`memory_bridge.py`) perform no authentication. They are designed for local use only. Do not expose port 8888 or the MCP server to the public internet.

### Stored prompt injection (unmitigated — see README §17)

Web-retrieved content ingested into the memory backend can contaminate `community_summaries` after consolidation. See the Open Problems section of the README for a full description and planned mitigations. Do not ingest untrusted external content at volume until those defences are implemented.

## Supported Versions

This project is in active development. Security fixes are applied to the latest commit on `main` only.
