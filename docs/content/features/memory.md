---
title: "Memory System"
weight: 6
---

# Memory System

Polyclaw includes an automatic memory consolidation system that converts recent conversations into persistent long-term memories. The agent can recall past interactions at any time.

![Memory recall in conversation](/screenshots/web-memorydemo-whathavewetalkedrecently.png)

## How It Works

### Chat Turn Buffering

During conversations, each message exchange is buffered in memory. The buffer accumulates turns until an idle period triggers consolidation.

### Idle-Triggered Consolidation

After `MEMORY_IDLE_MINUTES` (default: 5 minutes) of inactivity:

1. The buffered chat turns are collected
2. A dedicated LLM call (using `MEMORY_MODEL`) performs all of the following in a single pass:
   - Appends a **daily log entry** to `memory/daily/YYYY-MM-DD.md`
   - Creates or updates **topic notes** under `memory/topics/`
   - Updates the **agent profile** (`agent_profile.json`) with the user's emotional tone and any new preferences or facts learned
   - Increments **skill usage counters** (`skill_usage.json`) for every skill used during the conversation
   - Rewrites **suggestion queries** (`suggestions.txt`) with 4-6 contextually relevant follow-up questions
3. If proactive messaging is enabled, a **proactive follow-up** may be scheduled based on the conversation context
4. The buffer is cleared

### Memory Storage

```
~/.polyclaw/memory/
  daily/
    2025-02-17.md    # Daily log entries
    2025-02-16.md
  topics/
    project-alpha.md  # Topic-specific notes
    deployment.md
```

#### Daily Logs

Chronological summaries of each day's interactions. Includes timestamps, topics discussed, decisions made, and action items.

#### Topic Notes

Knowledge organized by subject. Accumulated over time as the agent encounters recurring topics.

## Inspecting Memory

![Inspecting memory in workspace](/screenshots/web-infra-inspectmemoryinworkspace.png)

### Context in Conversations

Memory context is available to the agent through:

- The `search_memories_tool` (when Foundry IQ is enabled)
- Direct file reading from the memory directory
- Inclusion in scheduled task prompts

### Proactive Messaging

The proactive message generator uses memory context to generate relevant follow-up messages based on recent conversations and topics.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORY_MODEL` | `claude-sonnet-4.6` | Model for memory consolidation |
| `MEMORY_IDLE_MINUTES` | `5` | Idle time before consolidation triggers |

## Foundry IQ Integration

![Foundry IQ memory integration](/screenshots/web-infra-memoryfoundryiq.png)

When Azure AI Foundry IQ is enabled, memories are indexed for semantic search:

1. Memory files are indexed periodically
2. The `search_memories_tool` performs vector search over indexed memories
3. Results include relevance scores and source file references

This provides more accurate memory retrieval than simple file-based lookups.
