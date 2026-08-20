---
name: n8n-house-rules
description: House rules, environment facts, and hard-won gotchas for building, reviewing, and debugging n8n workflows on Scott's self-hosted stack (Docker Desktop on WSL2, self-hosted-ai-starter-kit). Use this skill for ANY n8n work - writing or editing workflow JSON, configuring nodes, webhooks, Retell AI voice agent integration, Microsoft Outlook nodes, Postgres nodes, Ollama/local model agents, Docker or WSL2 issues affecting n8n, or troubleshooting workflow errors. Trigger whenever the user mentions n8n, a workflow, a node, Retell, Anthony, the starter kit, or pastes workflow JSON, even if they don't name this skill.
---

# n8n house rules

This skill exists because past builds failed in the same ways repeatedly: fabricated node types that cost three rebuild cycles, an environment variable bug that took a full debugging session to find, infinite loops from strict-equality checks on LLM output. Everything here was verified the hard way. Follow it.

## The one rule above all others

**Never write a node type name, typeVersion, parameter name, or configuration structure from memory.** Verify first, every time:

- If n8n-MCP tools are available (Claude Code sessions may have them): `search_nodes` → `get_node` → `validate_node` before writing config.
- If not (paste-JSON mode, the common case on Claude.ai): confirm the node type and parameters against docs.n8n.io by web search before writing it. If it can't be verified, say so and ask, don't guess.

Fabricated node types that have already burned time: `@n8n/n8n-nodes-retell.retellTrigger` (does not exist as a usable trigger), invented Outlook configurations, invalid workflow JSON structure. A plausible-looking node name is not evidence it exists.

## Build methodology

1. **Design before building.** Present the full node sequence and what each node does. Get confirmation before writing configuration.
2. **One operation per node.** Never combine DELETE and INSERT in one Execute Query node. Never stack multiple SQL statements. Each node does one thing.
3. **Postgres nodes:** Execute Query operation with parameterised queries (`$1`, `$2`), never string concatenation. Show what the query will do before the user executes it. The n8n internal database (`n8ndb`) never stores business data.
4. **Idempotency.** Every workflow must be safe to re-run without duplicating data.
5. **Build one node at a time** and verify output before connecting the next, unless the user asks for a full workflow in one pass.
6. **When reviewing an existing workflow, read the actual JSON**, not documentation prose about it. Extract the real code from Code nodes before reasoning about behaviour. Documentation drifts; the JSON doesn't.
7. **Workflow settings:** executionOrder v1 (connection-based).
8. Propose before executing anything with meaningful consequences.

## Logic patterns that have failed before

- **Webhook payload data lives under `$json.body`**, not `$json`. This is the single most common expression error.
- **Never use strict equality on LLM output.** Local models return "Finished.", "The blog is Finished.", etc. Use `contains`, case-insensitive. And always add an iteration counter with a hard cap (e.g. 3) on any evaluate-and-loop pattern, or it can loop forever.
- **Every IF node's false branch must route somewhere.** Empty false branches silently drop items. Decision checks run sequentially as a tree (check 1 → if false, check 2 → if false, check 3 → default route), never fired in parallel from one node.
- **Agent-type nodes require a tool-calling model.** This fails even if no tools are attached. If the step doesn't need tools, use a Basic LLM Chain instead.

## Environment and integrations

Two reference files carry the detail. Read the relevant one before touching anything it covers:

- **references/environment.md** — the Docker/WSL2 stack, environment variables (including the semicolon bug), PowerShell traps, safe Docker recovery commands. Read before any infrastructure, env var, file-access, or container work.
- **references/integrations.md** — Retell AI webhook patterns, Microsoft Outlook node configuration, Ollama/local model constraints, Cloudflare Tunnel. Read before any work touching Retell/Anthony, Outlook, or local LLM nodes.

## Project context

When working inside the UCES (exempt accommodation pipeline) project, additional authoritative material exists in project knowledge: six detailed n8n reference documents (validation expert, node configuration, expression syntax, MCP tools expert, workflow patterns, Code node guides), per-node documentation for every built pipeline workflow, and project instructions with database rules. Search project knowledge first there; those documents and the project instructions take precedence over this skill where they overlap. Outside that project, this skill is the standing record.

## After the build

Once a workflow is built and verified, offer to produce node documentation: one markdown file per node covering type, purpose, credential, full query/code/URL, logic, parameter mapping, re-run behaviour, connections, and verified output with date.
