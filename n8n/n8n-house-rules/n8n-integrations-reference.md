# Integrations: Retell AI, Outlook, Ollama, Cloudflare Tunnel

Patterns verified through the Anthony voice agent build and related work. Where a working reference exists, it's named; prefer copying a verified pattern over writing a new one.

## Retell AI (voice agent "Anthony")

- **There is no usable Retell trigger node.** The community package `n8n-nodes-retellai` is installed but the trigger node type `@n8n/n8n-nodes-retell.retellTrigger` is not recognised by n8n. All Retell-to-n8n communication uses standard `n8n-nodes-base.webhook` nodes. Do not invent Retell node types.
- Retell reaches self-hosted n8n through a **Cloudflare Tunnel** (DNS is on Cloudflare). Tunnel error 1033 means `cloudflared` is not running on the host machine; start it before debugging anything else.
- Direction of travel: Retell custom functions and webhooks call n8n webhook URLs with a JSON payload; n8n responds with the variables Retell's prompt states consume (recommended next action, category, etc.). Remember webhook payload fields are under `$json.body`.
- Anthony's qualification logic uses SONCAS motivation scoring with categories **HIGH / MEDIUM / LOW** (not "MID"). Scoring is Anthony's job during the call, from behavioural signals; n8n routes on the result, it does not re-score by keyword counting.
- **Vulnerability detection runs as an override checked first**, then category checks in sequence: Vulnerability → if false, HIGH → if false, MEDIUM → if false, route LOW. Every false branch routes somewhere. Vulnerability and motivation logging must both complete regardless of path.
- A fully curl-tested reference workflow exists: **WF2 `retell_anthony_call`** (JSON and README in UCES project knowledge). Check it before building anything Retell-adjacent.
- Known open item pattern: placeholder addresses (e.g. `escalations@slendeavours.com`) must be flagged and confirmed before any deployment.
- Telephony sits on Twilio elastic SIP trunking (Ireland IE1 region, UDP transport, localised termination URI). If calls fail silently after config changes, check the number's regional routing is Active on the Voice Configuration page; "Inactive" routing with a correctly assigned trunk was a past root cause.

## Microsoft Outlook

Verified against docs.n8n.io: node type `n8n-nodes-base.microsoftOutlook`, **typeVersion 2.2**.

- Calendar booking: `resource: "event"`, `operation: "create"`.
- Sending email: `resource: "message"`, `operation: "send"`.

Earlier builds fabricated other configurations for this node; use only these verified pairs, and re-verify against docs.n8n.io if a different resource/operation is needed.

## Ollama and local models in workflows

- **Agent-type nodes require a tool-calling model, even with zero tools attached.** DeepSeek R1 does not support tool calling and fails with `fetch failed` or similar. If the step needs no tools (e.g. an evaluator that only reads and responds), use a **Basic LLM Chain** node instead of an Agent node.
- `fetch failed` on an Ollama-backed node usually means memory. Two 14b models loaded simultaneously is too much on this hardware; standardise on one workhorse model (qwen2.5:14b has been the reliable choice) across agents in a workflow rather than mixing large models.
- Local models are unreliable at exact-string outputs. Any node checking an LLM's response uses `contains`, case-insensitive, never strict equality, and every evaluate-loop carries an iteration cap.
- Long generations need the raised HTTP timeout (set globally via `N8N_DEFAULT_HTTP_REQUEST_TIMEOUT`); if a large model times out, check this before swapping models.

## General webhook and CRM notes

- HubSpot is the CRM of record for inbound lead data; GDPR consent capture happens in the call flow before any marketing communication, and consent state is stored alongside the lead. Do not design flows that send nurture content without a consent check node.
- Test payloads: every decision path in a routing workflow gets its own documented test payload (the WF2 build documented four, one per path). Build these before activation, not after a live failure.
