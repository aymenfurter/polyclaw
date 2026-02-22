---
title: "Guardrails & Human-in-the-Loop"
weight: 8
---

# Guardrails & Human-in-the-Loop

Polyclaw includes a defense-in-depth guardrails framework that intercepts every tool invocation before execution and applies a configurable mitigation strategy. This gives operators fine-grained control over what the agent can do, how much autonomy it has, and how suspicious activity is detected and blocked.

![Guardrails settings](/screenshots/web-hardening-guardrails-intropage.png)

---

## Mitigation Strategies

Every tool call is evaluated against the active policy and routed to one of six strategies:

| Strategy | Channel | Behavior |
|----------|---------|----------|
| **Allow** | -- | Immediate permit. The tool executes without intervention. |
| **Deny** | -- | Immediate block. The tool call is rejected and logged to [Tool Activity](/features/tool-activity/). |
| **HITL** | Chat or Bot | Human-in-the-Loop. An approval prompt appears in the active chat or messaging channel. The operator has 300 seconds to approve or deny. |
| **PITL** (Experimental) | Phone | Phone-in-the-Loop. The agent calls a configured phone number for voice approval. |
| **AITL** | -- | AI-in-the-Loop. A separate Copilot session reviews the tool call for safety. |
| **Filter** | -- | Content analysis only. Azure AI Prompt Shields scan the input; the tool proceeds if clean. |

![AITL mitigation settings](/screenshots/web-hardening-guardrails-mitigatiosettings-AITL.png)

---

## Presets

Three built-in presets define a `(context, risk) -> strategy` matrix so you do not have to configure every tool individually.

**Interactive context:**

| Preset | Low Risk | Medium Risk | High Risk |
|--------|----------|-------------|-----------|
| **Permissive** | Filter | Filter | Filter |
| **Balanced** | Filter | Filter | HITL |
| **Restrictive** | Filter | HITL | HITL |

**Background context (scheduler, bot processor, realtime, etc.):**

| Preset | Low Risk | Medium Risk | High Risk |
|--------|----------|-------------|-----------|
| **Permissive** | Filter | Filter | HITL |
| **Balanced** | Filter | HITL | Deny |
| **Restrictive** | Filter | Deny | Deny |

A **Custom** option is available when you need full control over every tool and context.

### Risk Classification

Tools are automatically classified by risk level based on their name:

- **Low**: read-only operations (`view`, `grep`, `glob`)
- **Medium**: modification operations (`create`, `edit`)
- **High**: execution and external operations (`run`, `bash`, GitHub/Azure MCP servers)

### Model-Aware Policies

The effective preset is adjusted based on the model tier in use:

| Model Tier | Examples | Effective Preset |
|------------|----------|-----------------|
| Tier 1 (Strong) | `gpt-5.3-codex`, `claude-opus-4.6`, `claude-opus-4.6-fast` | Loosened by one step: balanced → permissive, restrictive → balanced |
| Tier 2 (Standard) | `claude-sonnet-4.6`, `gpt-5.2`, `gemini-3-pro-preview` | Preset used as-is; no adjustment |
| Tier 3 (Cautious) | `gpt-5-mini`, `gpt-4.1` | Tightened by one step: permissive → balanced, balanced → restrictive |

Tier 1 models are trusted enough to receive a more permissive posture than the selected preset. Tier 2 models receive the preset exactly. Tier 3 models are automatically tightened by one step, reducing the risk of unsafe actions from smaller models with weaker instruction following.

![Model-level policy matrix](/screenshots/web-hardening-guardrails-modellevelmatrix.png)

---

## Human-in-the-Loop (HITL)

When a tool call requires human approval, an interactive approval banner appears in the chat interface. The banner shows the tool name, its arguments, and Allow/Deny buttons. The operator has 300 seconds to respond before the request times out and is denied.

![HITL approval in chat](/screenshots/web-chat-humanintheloop.png)

HITL approval also works through bot channels (Teams, Telegram). When the agent is operating via a messaging channel, the approval request is sent as a message in the conversation.

---

## Phone-in-the-Loop (PITL) -- Experimental

> **Experimental:** PITL is under active development. The voice approval flow may change in future releases.

For scenarios where the operator is away from the dashboard, PITL calls a configured phone number using Azure Communication Services and the OpenAI Realtime API. A voice prompt describes the tool and its arguments, and the operator approves or denies by speaking.

---

## AI-in-the-Loop (AITL)

AITL spawns a separate Copilot session (default model: `gpt-4.1`) that acts as an independent safety reviewer. The reviewer evaluates the tool call for:

- Prompt injection
- Data exfiltration
- Destructive actions
- Privilege escalation

The reviewer uses a **spotlighting** defense technique that replaces whitespace with `^` in untrusted content, marking it as external input to prevent indirect prompt injection against the reviewer itself.

The review has a 30-second timeout. If the reviewer does not respond in time, the tool call is denied.

---

## Prompt Shields (Content Safety)

Azure AI Content Safety Prompt Shields provide an additional content analysis layer. When enabled, every tool invocation (or those matching the `filter` strategy) is scanned for prompt injection attacks before execution.

- Authentication uses Entra ID via `DefaultAzureCredential` (no API keys)
- **Fail-closed**: any API error is treated as an attack detection
- Bearer token caching with 5-minute refresh buffer
- Deployable from the Setup Wizard with a "Recommended" badge

Prompt Shields can be enabled alongside any other strategy. When combined with HITL or AITL, the shield check runs first -- if an attack is detected, the tool call is denied immediately without reaching the human or AI reviewer.

---

## Custom Rules

Beyond presets, you can define per-tool rules with:

- **Pattern**: regex matching tool names (e.g., `github-mcp-server`, `bash`)
- **Scope**: tool or MCP server
- **Action**: any mitigation strategy
- **Contexts**: which agent contexts the rule applies to (interactive, scheduler, bot, proactive, etc.)
- **Models**: which model tiers the rule applies to
- **HITL channel**: chat or phone

Rules are evaluated in order and override the preset matrix for matching tools.

---

## Red Teaming

The guardrails page includes a red teaming section for probing the agent's defenses. This lets you simulate adversarial prompts and verify that Prompt Shields and guardrail policies correctly detect and block attacks.

![Red teaming](/screenshots/web-hardening-redteaming.png)

---

## Context-Aware Policies

Background agent contexts (scheduler, bot processor, proactive loop, memory formation, AITL reviewer, realtime) each have metadata describing their purpose and risk profile. Policies can be configured per-context, allowing tighter controls for autonomous background operations while keeping interactive sessions more permissive.

---

## Policy Engine

Under the hood, the guardrails system generates YAML policies consumed by the [agent-policy-guard](https://github.com/agent-policy/guard) policy engine. The policy engine was externalized as its own open-source project so it can be used independently of Polyclaw. Advanced users can inspect and edit the raw YAML through the Template Inspector modal in the UI.
