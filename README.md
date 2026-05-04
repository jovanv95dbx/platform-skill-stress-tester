# Platform Skill Stress Tester

Stress-test Claude Code skills by running diverse AI personas against them on real cloud infrastructure. Each persona is a simulated customer with specific needs, cloud, region, and complexity. Independent Claude Code agents read the skills, talk to the customer through a relay, and deploy real infrastructure. Failures expose skill gaps.

```
BUILD skills → TEST with personas → ANALYZE failures → UPDATE skills → REPEAT
```

This is the framework that produced the [`ai-platform-kit`](https://github.com/jovanv95dbx/ai-platform-kit) skill iterations. Every iteration starts here, runs a stress test against the AI Platform Kit, and merges findings back.

## Architecture (one paragraph)

A small Python relay (port 11435) holds chat history per persona and routes between Claude agent ↔ Ollama. Each persona's system prompt is baked into a named Ollama model (`<slug>-persona`) so the SA agent never sees it. The relay is stateful: the agent posts its next reply, the relay calls Ollama with full history, and returns the customer's next reply. Each agent reads ALL skills, has its own cloud auth, and deploys real Terraform. Activity events stream to disk per persona for post-run analysis. See `SKILL.md` for the full breakdown including the persona-isolation rationale and the mandatory 3-path verification.

```
┌─────────────────────────────┐         ┌─────────────────────────────┐    terraform   ┌─────────┐
│  Ollama (port 11434)        │         │  Claude Code agent (opus)   │  ─────────────►│  Cloud  │
│  base: llama3.2:3b          │         │  one per persona            │  real infra    │ AWS/Az/ │
│  custom models:             │         │  reads ALL skills           │                │  GCP    │
│   alice-persona  ← system   │ ◄─────  │  POST localhost:11435/chat/ │                └─────────┘
│   bob-persona      prompts  │         │       <persona> {"text":...} │
│   ...                       │         └─────────────────────────────┘
└──────────────┬──────────────┘                          ▲
               │ /api/chat (full history each call)      │
               │                                         │
       ┌───────▼──────────┐    holds messages[]          │
       │ Persona Relay    │    per persona session       │
       │ (port 11435)     │ ─────────────────────────────┘
       │ stdlib Python    │  returns ONLY the customer's reply text
       │ no deps          │  → SA never sees system prompt or history
       └──────────────────┘
```

## Prerequisites

1. **Ollama** with a base model:
   ```bash
   brew install ollama
   brew services start ollama
   ollama pull llama3.2:3b
   ```

2. **Claude Code** CLI installed and signed in.

3. **A skill set to test** — typically a directory of `*.md` skill files at `<some-repo>/.claude/skills/`.

4. **Cloud credentials** for whichever cloud the personas will deploy to:
   ```bash
   aws sso login          # AWS
   az login               # Azure
   gcloud auth login      # GCP
   ```

5. **Databricks credentials** for workspace creation. For AWS, the recommended pattern is one Account-Admin service principal at the Databricks account level — see `SKILL.md` "Per-run scaffolding" and the [AI Platform Kit](https://github.com/jovanv95dbx/ai-platform-kit) `platform-provisioning/AWS.md` for the canonical SRA auth pattern.

6. **Permission settings.** Remove `aws*`, `curl *`, `terraform*` from any deny list in `~/.claude/settings.json` — agents inherit the lead's permissions.

## Quickstart

```bash
# 1. Clone and install (no install — pure Python stdlib)
git clone https://github.com/jovanv95dbx/platform-skill-stress-tester
cd platform-skill-stress-tester

# 2. Write your personas file (see templates/PERSONAS-FORMAT.md for the format)
mkdir -p personas
$EDITOR personas/my-run-personas.md

# 3. (AWS only — one-time per Databricks account)
#    Create the canonical Account-Admin service principal that authenticates
#    every Terraform provider via env vars. Saves creds to /tmp/canonical-sp/.
python3 scripts/setup_canonical_sp.py --profile <your-account-u2m-profile>

# 4. Build the per-persona Ollama models (one-time per run)
python3 templates/build_modelfiles.py --personas personas/my-run-personas.md

# 5. Generate per-agent spawn briefings
python3 templates/build_spawn_prompts.py \
    --personas personas/my-run-personas.md \
    --skills /path/to/skills-under-test/.claude/skills \
    --run-name my-run

# 6. Start the persona relay (stateful chat broker)
python3 scripts/persona_relay.py --port 11435 --log-dir logs/my-run &

# 7. (AWS) Source the canonical SP creds before spawning agents
source /tmp/canonical-sp/sourceme

# 8. From inside Claude Code, spawn one Agent per persona, pointing at:
#    personas/my-run-personas-corpus/spawn-prompts/<slug>-spawn.txt
#    Agent reads its briefing → reads skills → talks to relay → deploys → verifies
```

## Repo layout

```
platform-skill-stress-tester/
├── README.md                 (this file — quickstart + architecture)
├── SKILL.md                  (full framework documentation, including run playbook)
├── LICENSE                   (Apache 2.0)
├── scripts/
│   ├── persona_relay.py      (stateful chat broker — port 11435)
│   └── setup_canonical_sp.py (AWS one-time: creates Account-Admin SP for Terraform auth)
├── templates/
│   ├── build_modelfiles.py   (turns persona file into Ollama models)
│   ├── build_spawn_prompts.py (turns persona file into per-agent briefing files)
│   └── PERSONAS-FORMAT.md    (format spec — write your own personas, they're run-specific)
└── docs/
    └── architecture.md       (deeper explanation of persona isolation + verification mandate)
```

## What this is NOT

- **Not a deploy tool.** It deploys real infra as a side-effect, but the goal is to find skill bugs, not to provision production workspaces.
- **Not opinionated about which skills to test.** Pass any `.claude/skills/` directory.
- **Not free.** Personas trigger real `terraform apply` on real cloud accounts. The skill improvements pay for themselves but you should run this on a sandbox account, not prod.

## Why persona isolation matters

The SA agent must NOT see the persona's system prompt. If it does, the test is leaky — the agent gains hidden customer knowledge before the conversation begins, and the test is no longer measuring "what skills can the agent figure out from a real conversation". Two layers seal the leak:

1. **Ollama custom models** bake the persona prompt into the model itself. The SA never sees the prompt as text.
2. **Persona relay** holds the running message history per persona session. The SA only sees the latest customer reply.

See `SKILL.md` "Persona Isolation" for the threat model and `docs/architecture.md` for hardening tips.

## License

Apache 2.0. See `LICENSE`.

## Acknowledgements

Architecture inspired by red-team / persona-based testing patterns. Implementation refined across the [`ai-platform-kit`](https://github.com/jovanv95dbx/ai-platform-kit) iteration runs.
