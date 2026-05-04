# Architecture

## Goals

1. **Test skills, not models.** A skill stress test should reveal where the *skill prose* breaks down on real infrastructure. Model capability noise should be minimised — that's why we use Opus for every SA agent (consistent baseline) and a small local LLM for every persona (cheap, willing).
2. **Persona isolation.** The SA agent must NOT have privileged access to the persona's hidden cards (cloud, compliance, behaviour rules, conversation history). If it does, the test is leaky — the agent passes for the wrong reason.
3. **Real infrastructure.** Mocked deployments don't find IAM trust policy races, CIDR overlaps, or vending-machine metastore behaviour. Pay for the cloud resources — the skill improvements pay for themselves.
4. **Reproducibility.** Every conversation, decision, terraform call, error, and verification result is structured-logged to disk so post-run analysis doesn't depend on memory or tool output that's already scrolled past.

## Components

### Ollama custom models (one per persona)

Each persona is registered as a named Ollama model `<slug>-persona`, built via a `Modelfile`:

```
FROM llama3.2:3b
PARAMETER temperature 0.7
PARAMETER num_ctx 8192
SYSTEM """<the persona's full system prompt — the customer's hidden definition>"""
```

The persona system prompt is **baked into the model**. When Ollama is queried via `/api/chat` with `model: alice-persona`, it applies that system prompt automatically. The caller (the relay) doesn't need to supply it; the SA agent never sees it. This is **layer 1** of persona isolation.

Built once per run via `templates/build_modelfiles.py`.

### Persona relay (stateful chat broker)

A small stdlib-Python HTTP server on port 11435 (`scripts/persona_relay.py`) holds per-persona session state:

```
in-memory store: {
  "alice": [
    {"role": "user", "content": "<canonical opener>"},
    {"role": "assistant", "content": "<customer's first reply>"},
    {"role": "user", "content": "<SA's reply>"},
    {"role": "assistant", "content": "<customer's next reply>"},
    ...
  ],
  "bob": [...],
}
```

The SA agent posts its next reply text to `POST /chat/<slug>` and gets back the customer's next reply text. Internally, the relay calls Ollama's `/api/chat` with the full message history (Ollama is stateless across requests; the relay holds state).

**Crucially, the SA agent's view is one reply at a time.** It never sees the system prompt or the running history. This is **layer 2** of persona isolation.

The relay also **persists every chat turn to disk** at `<log-dir>/<persona>-chat.jsonl` (via `--log-dir`), so the full conversation can be replayed/analysed post-run without depending on Ollama's in-process state.

### Per-agent spawn briefings

Each SA agent gets a per-persona briefing file at `personas/<run>-corpus/spawn-prompts/<slug>-spawn.txt`. The briefing contains:

- Cloud auth instructions (profile names, account/subscription IDs)
- Skills directory path (read all `*.md`, no filtering)
- Activity-logging mandate (write JSONL events to `logs/<run>/<slug>-activity.jsonl`)
- Verification mandate (the 3-path rule)
- Lessons-learned (stable operational fixes from prior runs)
- Step-by-step workflow

**The briefing does NOT contain the persona system prompt.** That stays inside the Ollama model.

Generated per run via `templates/build_spawn_prompts.py`.

### Claude Code SA agents (one per persona)

Spawned from a top-level Claude Code session via the `Agent` tool with `mode: bypassPermissions`, `model: opus`, and a short pointer to the briefing file. Each agent reads its briefing, reads ALL skill files in the target skills directory, opens a relay conversation, deploys real Terraform on the persona's cloud, runs the 3-path verification, writes a transcript + summary, and reports back to the team-lead.

All agents run in parallel. Resource isolation: each agent writes to its own `deployments/<run>/<slug>/` directory; lock files are per-state-file.

## Persona isolation — what the SA agent sees and doesn't

| The SA agent sees | The SA agent never sees |
|---|---|
| Its briefing file (cloud auth, skills dir, relay endpoint, lessons learned) | The persona's system prompt text |
| Skill files at `<skills-dir>/**/*.md` | The persona's behaviour rules ("when X, say Y") |
| The customer's spoken text (returned from `/chat/<slug>` calls, ONE TURN AT A TIME) | The full message history |
| Cloud CLI output | The corpus dir (`personas/<run>-corpus/`) — Modelfiles, system prompt files, etc. |
| Terraform plan/apply output | Anything from other personas |

## Verification mandate (the 3-path rule)

After every workspace + UC deployment, the SA agent MUST run all three compute paths against a UC table:

1. **Classic cluster** with UC (`data_security_mode = "SINGLE_USER"`) — surfaces cluster policy bugs, init scripts, custom AMIs, SCC ports, JVM warmup, IAM propagation
2. **Serverless SQL warehouse** (PRO, 2X-Small) — surfaces serverless egress / storage credential issues
3. **Serverless notebook job** (Python with `dbutils.notebook.exit(...)`) — surfaces a different identity context + egress path

Each path: `CREATE TABLE → INSERT → SELECT → DROP`. SELECT must return the row inserted. Save results to `verification.json` and `verification.log` in the persona's deployment dir.

Why all three: classic cluster is where most real-world skill bugs surface. Agents naturally reach for serverless because it's the cheapest path — that bias is exactly why the mandate exists. Skipping classic = skipping the test.

## Hardening notes

- The relay binds to 127.0.0.1 by default. Don't expose it to the public network — the SA agent sends raw SA replies to it.
- **Move system-prompt source files OUT of the project tree** if you're worried about a curious SA agent reading them. E.g. write them to `~/.cache/persona-internals/` and feed them to `ollama create` from there. The relay doesn't need the source files at runtime.
- **The "do not read corpus" rule must be in the spawn briefing.** Without it, a curious SA might read its own corpus dir and see the persona prompt as text. The briefing template in `templates/build_spawn_prompts.py` includes this rule.
- **Use opus for SA agents.** Cheaper models follow instructions less faithfully and you'll get model-capability noise mixed into your skill findings.

## Why not just give the SA agent a system prompt that says "play this customer scenario"?

Because the entire point is to test what the agent figures out from a *real* conversation, not a script. If the agent has the customer's behaviour rules in its own context, it'll pre-empt them — answering questions before the customer asks, never hitting the contradiction the persona was designed to surface, etc. The tests look easier than they are.

The persona-isolation pattern (Ollama model + relay) is the smallest hack that mechanically prevents that leak: there's no way for the SA's context to contain the persona's rules unless it actively reads files it's been told not to read.
