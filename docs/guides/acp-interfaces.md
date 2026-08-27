# ACP interfaces: running crews on different agent harnesses

By default every crew runs on **kiro-cli**, and nothing here needs configuring.
This page is for the case where you want *some* crews on a different ACP
harness — a local model behind an ACP adapter, or a second vendor's agent
binary — while the rest stay on kiro-cli.

## The two nouns

| Noun | What it is |
|---|---|
| **Interface** | What you select. A named harness: the built-in `kiro-cli` and `kas`, plus any you declare. |
| **Backend** | What the code speaks. A closed set of protocol dialects. You do not choose one directly. |

They are separate because the dialects are a fixed set the product has to
understand, while interfaces are open-ended: you may declare as many external
harnesses as you like, each with its own command and environment.

## Declaring an interface

`acp_interfaces` in `config.json`:

```json
{
  "acp_interfaces": {
    "lmstudio": {
      "command": "/Users/you/bin/acp-lmstudio",
      "args": ["acp", "--agent", "{agent}"],
      "env": { "LMSTUDIO_MODEL": "qwen3-coder" },
      "description": "Local model via an ACP adapter"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `command` | **Required.** Absolute path to the backend executable. An entry without one is dropped at load with a warning, so the mistake surfaces at startup rather than on a crew's first message. |
| `args` | Argv after the command. `{agent}` and `{model}` are substituted. Omitted means the kiro-cli convention, `["acp", "--agent", "{agent}"]`. |
| `env` | Extra environment for the process. Kiro's own API key is never passed to an external harness regardless of what you put here. |
| `description` | Free text. |

`kiro-cli` and `kas` are built in and always available. Declaring an interface
with either name is refused: a redefined `kiro-cli` would silently move every
unconfigured crew onto your command.

## Binding a crew

```json
{
  "agents": {
    "day":   { "kiro_agent": "kirocrew" },
    "night": { "kiro_agent": "kirocrew", "acp_interface": "lmstudio" }
  }
}
```

`day` runs on kiro-cli, `night` runs locally, in the same gateway at the same
time. Subagents inherit their parent's interface.

Resolution order, highest first:

1. the crew's `acp_interface`
2. the global `agent.acp_interface`
3. the pre-existing `agent.acp_backend` (so an install that set `kas` keeps
   working without being re-configured)
4. `kiro-cli`

An unknown interface name falls back to `kiro-cli` with a warning rather than
failing the session — a typo in one crew's binding must not take the install
down.

## What an external harness must implement

ACP over stdio, protocol version `2025-08-22`:

- `initialize` → return `protocolVersion` and `agentCapabilities`
- `session/new` → return a `sessionId` (**required**; its absence is fatal)
- `session/set_mode`, `session/set_model` → reply `{}` (neither is awaited)
- `session/prompt` → stream `session/update` notifications, then return a
  `stopReason` of `end_turn` / `cancelled` / `max_tokens` / `refusal`
- `session/cancel` (a notification) → acknowledge with `stopReason: "cancelled"`
- exit on stdin EOF

Optional but worth emitting: `agent_message_chunk` (streamed text),
`agent_thought_chunk` (reasoning), `tool_call` / `tool_call_update` (status
pills), `usage_update` (context meter), and `session/request_permission` —
see the security note below.

## Security

Three things are deliberately **not** granted to an external harness:

- **Crew's OS sandbox still applies.** Membership in
  `ACP_BACKENDS_INTERNAL_SANDBOX` makes `wrap_argv` *skip* Kiro Crew's seatbelt
  in deference to a harness's own internal sandbox. Only kiro-cli is a member.
  An external harness has demonstrated no internal sandbox, so it keeps Crew's.
- **Kiro's API key is stripped** from the child environment.
- **No kiro-cli capabilities are inherited**: session sharing, mid-turn steer,
  the `cli.json` effort / Tool Search overlay, and the kiro identity store are
  all opt-in memberships an external harness is not in.

**Tool gating is the harness's responsibility to invoke.** Kiro Crew's deny
engine and approval policy run on the `session/request_permission` path. A
harness that executes tools without asking bypasses them — not because the
floor failed, but because it was never consulted. Request permission before
every tool call, and put the literal command in `rawInput.command` so the
shell-command analysis sees what kiro-cli would show it.

**Readiness is still computed from kiro-cli.** The prerequisite service probes
`--version` and `whoami` against kiro-cli and gates the gateway on the result;
it does not yet probe interfaces independently. An install with no working
kiro-cli is reported not-ready even if its crews all run elsewhere.
