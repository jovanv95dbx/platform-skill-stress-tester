---
name: skill-stress-tester
description: "Stress-test any Claude Code skill set by running AI personas against them on real infrastructure. Uses local LLMs (Ollama) for cheap persona roleplay and independent Claude Code sessions for skill execution. Implements the Build → Test → Analyze → Update loop for continuous skill improvement."
---

# Skill Stress Tester

Systematically test Claude Code skills by running diverse AI personas against them on real cloud infrastructure. Each persona is a simulated customer with specific needs, cloud, region, and complexity. Claude Code sessions read the skills, talk to the customer, and deploy real infrastructure. Failures expose skill gaps.

## The Loop

```
BUILD skills → TEST with personas → ANALYZE failures → UPDATE skills → REPEAT
```

## Architecture

**Primary method: Agent Teams + persona isolation via Ollama models + relay.** Each persona gets its own Claude Code teammate session. The teammate plays the SA. A pre-built Ollama custom model + a stateful relay play the customer — and crucially the SA never sees the persona definition or the running message history.

```
┌──────────────────────────────┐         ┌──────────────────────────────┐    terraform   ┌─────────┐
│  Ollama (port 11434)         │         │  Claude Code teammate (opus) │  ─────────────►│  Cloud  │
│  base model: llama3.2:3b     │         │  one per persona             │  real infra    │ AWS/Az/ │
│  custom models:              │         │  reads ALL skills            │                │  GCP    │
│   aaron-persona              │         │  AUTH: own profile per cloud │                └─────────┘
│   sienna-persona  ← system   │         │                              │
│   karim-persona     prompts  │         │  Customer talk-to-me path:   │
│   ... (one per persona)      │ ◄────── │  POST localhost:11435/chat/  │
│                              │         │       <persona> {"text":...} │
└──────────────┬───────────────┘         └──────────────────────────────┘
               │                                          ▲
               │ /api/chat (full messages history each call)
               │                                          │
       ┌───────▼──────────┐    holds messages[]           │
       │ Persona Relay    │    per persona session       │
       │ (port 11435)     │ ─────────────────────────────┘
       │ Python stdlib,   │  returns ONLY the customer's reply text
       │ no deps          │  → SA never sees system prompt or history
       └──────────────────┘

       ┌──────────────┐
       │  Team Lead   │  creates team, spawns N teammates, assigns tasks
       │  (you/Claude)│  monitors progress (TaskList), broadcasts fixes (SendMessage)
       └──────────────┘
```

### How It Works (Agent Teams — primary method)

**Pre-flight (one-time per run):**

1. Enable agent teams: `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in settings.json
2. Build per-persona Ollama models (per run — see "Per-run scaffolding"): registers `<name>-persona` for each persona
3. Start the persona relay: `python3 scripts/persona_relay.py --port 11435 &` (stateful chat broker)
4. Build per-teammate spawn prompts (per run): writes `<slug>-spawn.txt` files — each contains skills dir + auth + relay endpoint, NOT the persona system prompt

**Run:**

5. Create a team with `TeamCreate`
6. Create one task per persona with `TaskCreate`
7. Spawn one teammate per persona via `Agent` tool with `team_name`, `model: opus`, `mode: bypassPermissions`. Pass a short prompt that points to the briefing file: `Read /Users/.../<slug>-spawn.txt and follow it precisely.`
8. Each teammate reads its briefing → reads ALL skill files from the skills directory
9. Each teammate calls the relay (`POST /chat/<persona>`) for customer messages — never sees the persona prompt
10. Each teammate follows the skills to deploy real infrastructure
11. Each teammate runs the **mandatory three-path verification** (classic + serverless SQL + serverless notebook) — see "Verification Mandate" below
12. Each teammate saves its transcript + `verification.json` + `verification.log` to its dedicated output directory
13. Team lead monitors via `TaskList`, nudges stuck agents via `SendMessage`, broadcasts fixes
14. All teammates run in parallel — 10 personas = 10 concurrent sessions


### Agent Teams vs Subprocesses (learned from AWS run)

| | Agent Teams | Subprocesses |
|---|---|---|
| **Parallel** | Native — each teammate is independent | ThreadPoolExecutor, fragile |
| **Tool access** | Full (Bash, Read, Write, etc.) | Needs `--mcp-config '{"mcpServers":{}}' --strict-mcp-config` to avoid MCP auth popups |
| **Permissions** | Inherits from lead. Set `bypassPermissions` mode | Needs `--allowedTools` flag |
| **Monitoring** | Real-time — teammates message the lead | Silent until done (unless streaming) |
| **Recovery** | Send fix instructions via `SendMessage`, respawn dead agents | Must restart entire script |
| **State** | Each teammate maintains its own session state | Stateless per invocation, needs `--resume` for multi-turn |

### Key Design Rules

- **ALL skills loaded.** Every .md file in the skills directory goes into every Claude session — Azure, AWS, GCP, everything. Claude must figure out from the customer's messages which cloud they're on and which skill files to follow. DO NOT filter skills by cloud. That defeats the test.
- **Always opus.** We're testing skills, not model capability. Opus follows instructions most faithfully. Using a cheaper model introduces model-capability noise into results.
- **Real infrastructure.** Personas trigger real `terraform apply` on real cloud accounts. Mocked deployments don't find real bugs.
- **Persona isolation is mandatory.** The SA agent MUST NOT see the persona system prompt or the running message history. Use the `*-persona` Ollama models (system prompt baked into the model) + the relay (port 11435, holds messages[]). The SA only sees what the customer says. If the SA reads the persona definition file, the test is leaky — the SA gains hidden customer knowledge before the conversation begins. See "Persona Isolation" section below.
- **Three-path verification is mandatory.** After every workspace deployment, the SA MUST run all three compute paths (classic cluster + serverless SQL warehouse + serverless notebook job) against a UC table. One serverless test does not count as verified. See "Verification Mandate" below.
- **Analysis is collaborative.** After a run, do NOT auto-analyze or auto-fix skills. The human reviews transcripts and decides what to fix. The analysis and skill updates are done together.

### Persona Isolation (CRITICAL — don't skip)

The SA agent must be functionally blind to the customer's hidden cards (their cloud, their compliance level, their behavior rules) until they surface in conversation. Two layers seal the leak:

**Layer 1 — Ollama custom models.** Each persona is registered as a named Ollama model (`<name>-persona`) via Modelfile. The persona system prompt is baked into the model itself, not into any request body. Build them once per run via the run's modelfile builder (see "Per-run scaffolding"). Verify with `ollama list` — should show one `*-persona` entry per persona.

**Layer 2 — Persona Relay.** A stateful Python broker on port 11435 holds the message history per persona. The SA hits `POST /chat/<persona>` with its next reply text and gets back the customer's next reply. The relay forwards the full history to Ollama on every call (Ollama is stateless), but the SA never sees the history.

**What the SA NEVER sees** (and the briefing must NOT contain):
- The persona system prompt text
- The persona's behavior rules ("when X, say Y")
- The full message history
- Anything from the run's corpus dir (`personas/<run>-corpus/spawn-prompts/*-system.txt`, `Modelfile_*`, the persona file itself)

**What the SA DOES see:**
- The skills it reads from disk
- Cloud auth instructions for its target cloud
- Lessons-learned (operational fixes from prior runs)
- The customer's spoken text (returned from `/chat/<persona>` calls)

**Hardening (optional, full lockdown):** Move the system-prompt source files OUT of the project tree (e.g. to `~/.cache/persona-internals/`) so a curious SA can't `cat` them. Add a "do not run `ollama show`, do not read files under `personas/<run>-corpus/`" rule to the spawn briefing. The relay alone closes the *passive* leak (briefing no longer embeds the prompt); these extras close the *active* leak.

### Verification Mandate (CRITICAL — don't skip)

After every workspace + UC deployment, the SA MUST run **all three compute paths** against a UC table — not just the cheapest one. The platform-kit's `deployment-verification` skill defines the protocol:

1. **Classic cluster** with UC (`data_security_mode = "SINGLE_USER"`, single-node)
2. **Serverless SQL warehouse** (PRO, 2X-Small)
3. **Serverless notebook job** (ephemeral job compute, Python notebook with `dbutils.notebook.exit(...)`)

Each path: `CREATE TABLE → INSERT → SELECT → DROP`. SELECT must return the row inserted. Save results to `verification.json` and `verification.log` in the persona's deployment dir. Cleanup test resources at end.

**Rationale.** Classic cluster is where most real-world skill bugs surface (cluster policies, init scripts, custom AMIs, SCC/PrivateLink port story, JVM warmup, `data_security_mode` enforcement). Agents naturally reach for serverless because it's the cheapest path — that bias is exactly why the mandate exists. Skipping classic = skipping the test. Empirical cold-start on simple workspaces is 3–6 min, not 10–15 — the cold-start warning is not a valid skip reason.

The only valid skip reasons are documented in `deployment-verification/SKILL.md` (persona explicitly forbids serverless, region doesn't support a path, etc.). Any skip MUST be logged with `"status": "SKIPPED-WITH-REASON"`.

## Prerequisites

1. **Ollama installed** with a model pulled:
   ```bash
   brew install ollama
   brew services start ollama
   ollama pull llama3.2:3b
   ```

2. **Skills to test** — a directory of .md skill files (e.g., `ai-platform-kit/.claude/skills/`)

3. **Cloud credentials** — logged into whatever cloud the personas deploy to:
   ```bash
   aws sso login          # AWS
   az login               # Azure
   gcloud auth login      # GCP
   ```
   **CRITICAL (AWS):** Credentials must be in `~/.aws/credentials`, not just exported as env vars. Teammate sessions don't inherit shell env vars. Write creds to the file:
   ```bash
   aws configure set aws_access_key_id <KEY> --profile default
   aws configure set aws_secret_access_key <SECRET> --profile default
   aws configure set aws_session_token <TOKEN> --profile default
   ```
   For mid-run refresh: just re-run the same commands with new creds. Teammates pick them up on next terraform call.

4. **Databricks credentials** — for workspace creation, you need a Databricks account-level profile in `~/.databrickscfg`. U2M (browser-cached) auth works best. M2M OAuth secrets obfuscated with `dose` prefix by the CLI do NOT work with Terraform.

5. **Personas written** — a .md file with persona definitions. Can be single or multi-persona.

6. **Permission settings** — remove `aws*`, `curl *`, `terraform*` from the deny list in `~/.claude/settings.json`. Teammates inherit the lead's permissions. If these are denied, every teammate gets blocked.

7. **Agent teams enabled** (for agent teams mode):
   ```json
   // ~/.claude/settings.json
   { "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" } }
   ```

## How to Run (Agent Teams — full playbook)

### Step 0: Pre-flight checks

Before spawning anything, verify:

```bash
# 1. Ollama running with base model
ollama list  # should show llama3.2:3b

# 2. Cloud creds — auth pattern:
#    AWS:    `aws sso login --sso-session <session>` then write the temp creds OR rely on the SSO cache
#            via AWS_PROFILE in the spawn prompt (cleaner, doesn't overwrite ~/.aws/credentials).
#    Azure:  `az login` (subscription + tenant should match what the persona's auth section expects)
#    Databricks: U2M browser-cached profile in ~/.databrickscfg. Avoid M2M with "dose"-prefix obfuscated
#                secrets — they don't work with Terraform.

# 3. No blocked tools in settings
cat ~/.claude/settings.json     # check deny list — remove aws*, curl*, terraform* if present

# 4. Agent teams enabled
# settings.json must have: "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" }

# 5. Per-run scaffolding (each run builds its own — see "Per-run scaffolding" below):
#    a. Write personas/<run>-personas.md  (one block per persona under ## Persona NN: headers)
#    b. Build modelfiles → ollama create <name>-persona  (one custom Ollama model per persona)
#    c. Build spawn-prompt briefings (skills dir + cloud auth + relay endpoint, NOT the persona prompt)
#    d. Start the relay:  python3 scripts/persona_relay.py --port 11435 &

ollama list                              # verify *-persona models exist
curl -s http://localhost:11435/health    # → {"ok": true}
```

### Step 1: Per-run scaffolding

Each new stress run is its own corpus directory under `personas/` (e.g. `personas/<run-name>-corpus/`). The run scaffolding is regenerated per run — these files are **not** stable framework artifacts. The framework artifacts that ARE stable are: this `SKILL.md`, `scripts/persona_relay.py`, and the platform-kit skills the run will test.

A run's scaffolding contains:

1. **The persona file** (`personas/<run-name>-personas.md`) — one block per persona under `## Persona NN:` headers. The persona-system-prompt body inside the code block is what becomes the customer's hidden definition. Ground the personas in real customer asks (ASQs from SFDC, Slack threads, ES tickets, RFP questions) rather than synthetic ones.

2. **A modelfile builder** (e.g. `personas/<run>-corpus/build_modelfiles.py`) — extracts each persona's system prompt, writes a `Modelfile_<slug>`, and runs `ollama create <name>-persona -f Modelfile_<slug>`. Minimal Modelfile body:
   ```
   FROM llama3.2:3b
   PARAMETER temperature 0.7
   PARAMETER num_ctx 8192
   SYSTEM """<persona system prompt>"""
   ```
   Result: N named Ollama models, each with a baked-in persona prompt that the SA never sees.

3. **A spawn-prompt builder** (e.g. `personas/<run>-corpus/build_spawn_prompts.py`) — generates per-teammate briefing files at `personas/<run>-corpus/spawn-prompts/<slug>-spawn.txt`. Each briefing contains: cloud auth instructions (profile names, account/subscription IDs), the skills directory path, the THREE-PATH verification mandate (see `deployment-verification` skill), lessons learned (auth gotchas), and the relay endpoint (`POST http://localhost:11435/chat/<persona>`). **Crucially: the briefing does NOT contain the persona system prompt.** That stays inside the Ollama model.

4. **Run dirs** (auto-created): `deployments/<run-name>/<persona>/` for Terraform per persona; `transcripts/<run-name>/<persona>.md` for conversation logs + verification artifacts.

### Step 2: Create team and tasks

```
TeamCreate: team_name = "<cloud>-stress-test-<date>"
TaskCreate: one per persona (e.g., "Stress test persona: <slug> (<cloud> <complexity>)")
```

### Step 3: Spawn teammates

Spawn one teammate per persona via the `Agent` tool. Each teammate gets:
- `team_name` — the team you created
- `mode` — `bypassPermissions`
- `model` — `opus`
- `name` — the persona slug (lowercased first name from the persona file)
- `prompt` — a SHORT pointer to the briefing file (does NOT inline the briefing — keeps the lead's context clean)

**Teammate spawn-call prompt (kept short):**

```
You are <name>, a Databricks SA in an ASQ-driven stress test. Your full briefing is in:

/Users/jovan.visnjic/vibe/skill-stress-tester/personas/<run>-corpus/spawn-prompts/<slug>-spawn.txt

READ THAT FILE FIRST, IN FULL, and follow every instruction in it precisely. It contains your
cloud auth instructions, the skills directory to read, the relay endpoint for talking to the
customer, lessons learned, and the step-by-step workflow.

When you complete the conversation + deployment + 3-path verification + transcript, mark task #N
as completed via TaskUpdate, then send a short summary to team-lead via SendMessage.
```

**The briefing file (`<slug>-spawn.txt`) — generated by `build_spawn_prompts.py` — contains:**

```
You are <name>, a Databricks SA. You're one of N teammates in a stress test of platform-kit skills.

Your job: have a NATURAL conversation with a simulated customer (played by Ollama via the persona
relay), then deploy REAL infrastructure based on what they need, then run the 3-path verification.

==============================
TARGET CLOUD + AUTH (CRITICAL)
==============================
Cloud: <AWS|Azure>
<cloud-specific auth: profile names, account IDs, tenant IDs, key prefixes to avoid>

==============================
SKILLS DIRECTORY — READ ALL FILES, NO FILTERING
==============================
Skills root: /Users/.../databricks-platform-kit-v2/.claude/skills/
Read EVERY .md file under that directory recursively. ALL of them.

==============================
LESSONS LEARNED (apply immediately, don't relearn)
==============================
- M2M OAuth creds with "dose" prefix DO NOT work with Terraform. Use U2M.
- Always prefix terraform with `env -u DATABRICKS_CLIENT_ID -u DATABRICKS_CLIENT_SECRET -u DATABRICKS_ACCOUNT_ID`.
- [AWS] Root S3 bucket policy needs arn:aws:iam::414351767826:root + BucketOwnerPreferred.
- Classic clusters need `data_security_mode = "SINGLE_USER"` for UC access.
- For workspace-level Terraform without browser auth: create an account-level SP, then use the
  account-SDK call `ac.workspace_assignment.update(ws_id, sp_id, [WorkspacePermission.ADMIN])`.
  The CLI says "Permission assignment APIs are not available" but the SDK call works.

==============================
STEP-BY-STEP WORKFLOW
==============================
STEP 1 — Read every .md under <skills-dir> (recursively).

STEP 2 — Open the conversation. Call the persona relay:
  curl -s -X POST http://localhost:11435/chat/<persona> \
       -H 'Content-Type: application/json' -d '{}'
  → returns {"persona":"<persona>","turn":1,"customer":"..."}
  Use the customer text as the customer's first message.

STEP 3 — Have a NATURAL conversation. For each subsequent customer turn, post your SA reply:
  curl -s -X POST http://localhost:11435/chat/<persona> \
       -H 'Content-Type: application/json' -d '{"text":"<your SA reply>"}'
  → returns {"persona":"<persona>","turn":N,"customer":"<next customer message>"}

  Conversation length is natural: 3-15 turns. Stop when the customer accepts the plan AND
  you've successfully deployed + verified, OR when you hit a hard blocker.

STEP 4 — DEPLOY. Real Terraform, real cloud account. Write all .tf to YOUR deployment dir.
  Run init/plan/apply (with the env -u prefix above). Don't stop on errors — diagnose and retry.
  Don't deploy more than the customer asked for. ONE workspace unless they explicitly ask for more.

STEP 4.5 — VERIFY (MANDATORY, all three paths).
  Read deployment-verification/SKILL.md and run all THREE compute paths against a UC table:
    1. Classic cluster (`data_security_mode = "SINGLE_USER"`, single-node, smallest node type)
    2. Serverless SQL warehouse (PRO, 2X-Small)
    3. Serverless notebook job (Python notebook with `dbutils.notebook.exit(...)`)
  Save verification.json and verification.log to <deployment-dir>.
  Classic cluster cold-start is 3-6 min on simple workspaces. Start it EARLY in step 4.

STEP 5 — Save transcript to <transcript-path>
  Per-turn customer + SA messages, deployment summary, skill gaps + observations.
```

**Include the real cloud account ID and Databricks account ID** in the auth section so the agent doesn't use the persona's fake IDs. Get these from `aws sts get-caller-identity`, `az account show`, and `~/.databrickscfg`.

### Step 4: Monitor and fix

- Use `TaskList` to check progress
- Use `SendMessage` to nudge stuck agents or broadcast fixes
- When one agent finds a workaround, send it to all stuck agents
- Dead agents (no response, no artifacts): respawn with lessons baked in

### Step 5: Collect results

When all teammates report done:
- Check `transcripts/<cloud>/` for all 10 transcripts
- Check `deployments/<cloud>/` for terraform state files
- Shut down completed teammates via `SendMessage` with `shutdown_request`
- Clean up team via `TeamDelete`

### Step 6: Review together

Do NOT auto-analyze. Wait for the user. Look at transcripts together:
- Which personas passed/failed verification?
- What errors did agents hit? Are they in the skill's error handling table?
- Did agents pick the right cloud-specific skill files?
- What self-corrections happened?
- What's missing from the skills?

### Step 7: Update skills together

User decides priority. Apply fixes to the skill files. Re-run failed personas to close the loop.

## How to Write Personas

A persona is a system prompt that tells Ollama who to roleplay as. The customer's cloud, region, and requirements all come from the persona — Claude must discover them through conversation.

### Required Elements

1. **Name and role** — "You are <FirstName> <LastName>, a <role> at <company>" (synthetic or anonymised — never embed real customer names you can't ship)
2. **Situation** — what they need, why, by when
3. **Technical context** — cloud, region, account IDs, existing infrastructure
4. **Knowledge level** — expert? beginner? what do they know/not know?
5. **Behavior rules** — how they respond to specific questions

### Behavior Rules Are Key

Behavior rules make personas predictable and testable. They define how the persona responds to the skill's intake questions, ensuring the conversation hits the skill paths you want to test.

```
Your behavior:
- When asked about environment strategy: "Just one workspace for now"
- When asked about tags: "owner=<email>"
- If asked too many questions: get impatient, say "just go with defaults"
- When shown the plan: accept it and say "looks good, let's do it"
```

### Complexity Levels

| Level | What It Tests | Example |
|-------|--------------|---------|
| **Low** | Happy path, defaults | First-time user, POC, "just make it work" |
| **Medium** | Custom requirements, cost concerns | Migration, specific naming, budget constraints |
| **High** | Enterprise features, compliance | Private Link, CMK/KMS, HIPAA, FedRAMP, multi-env |
| **Expert** | Code quality, modularity, edge cases | Terraform expert critiquing structure |

### Multi-Persona File Format

See `templates/PERSONAS-FORMAT.md` for the full format spec. One markdown file contains multiple personas separated by `## Persona NN:` headers, with each persona's system prompt as the first fenced code block in its section.

### Writing for Different Clouds

The persona's system prompt determines which cloud Claude needs to deploy to. Include cloud-specific details:

**AWS personas** — AWS account IDs, regions (us-east-1, eu-west-1), VPC CIDRs, S3 buckets, IAM roles, KMS key ARNs
**Azure personas** — subscription IDs, tenant IDs, regions (westeurope, eastus2), VNet CIDRs, ADLS storage accounts, Key Vault names
**GCP personas** — project IDs, regions (us-central1, europe-west1), VPC networks, GCS buckets, service accounts

The persona should mention their cloud naturally: "I'm on AWS in us-east-1" or "We use Azure, West Europe region."

## Existing Personas

None bundled. Personas are domain- and run-specific — write fresh personas for each run, ideally grounded in real customer asks (ASQs from SFDC, Slack threads, ES tickets, RFP questions). Mining a corpus of real asks, bucketing by complexity / cloud / use-case, and synthesizing composite personas from each bucket is the recommended pipeline. See `templates/PERSONAS-FORMAT.md` for the file-format spec and "Per-run scaffolding" below for how the persona file feeds the rest of the run.

## Known Issues / Lessons Learned

These are problems encountered during runs that are NOT skill bugs — they're operational issues with the stress tester itself.

### Agent teammates dying / going unresponsive
Some teammates die mid-run without reporting. Check for transcripts and deployment dirs. If missing, respawn with lessons learned baked into the prompt.

### Credential propagation
Shell env vars (export AWS_ACCESS_KEY_ID=...) do NOT propagate to agent team teammates. Write creds to `~/.aws/credentials` file instead. For Databricks, use profile-based auth in `~/.databrickscfg`.

### Permission denials blocking teammates
If `~/.claude/settings.json` has `aws*`, `curl *`, `terraform*` in the deny list, ALL teammates are blocked. Check and remove before running.

### M2M OAuth secrets (dose prefix)
The Databricks CLI obfuscates M2M secrets with a `dose` prefix. These do NOT work with Terraform. Use U2M auth (browser-cached) for account-level, or pass original non-obfuscated M2M secrets via env vars for workspace-level.

### Workspace-level auth on AWS
Account-level U2M tokens do NOT work for workspace APIs on AWS. For each workspace, run `databricks auth login --host <workspace-url>` to cache a workspace-level token, or use M2M with explicit DATABRICKS_HOST + CLIENT_ID + CLIENT_SECRET env vars.

### Terraform state locks from parallel runs
Multiple teammates deploying to the same directory causes state lock conflicts. Each persona MUST deploy to its own directory (e.g., `deployments/real/<persona>/`).

### Lessons-learned propagation
When one teammate finds a fix (e.g., bucket policy, auth workaround), broadcast it to other stuck teammates via `SendMessage`. This saved hours on the AWS run. Bake known fixes into respawn prompts for dead agents.

### Persona system prompt leaks
Inlining the persona system prompt directly in the teammate's spawn briefing lets the SA "see the customer's hidden cards" before the conversation starts — neutralizes behaviour-rule traps and makes conversation findings unreliable. **Never inline the persona prompt in the briefing**, even "for convenience." Use the Ollama-model + relay pattern instead. See "Persona Isolation" section.

### Three-path verification skipping
Letting agents declare "verified" after a single serverless SQL test systematically misses classic-cluster bugs (cluster policies, init scripts, custom AMIs, SCC ports, JVM warmup). The skill set under test should include a `deployment-verification` skill with a multi-path mandate, and the spawn-prompt template should include a `STEP 4.5 — VERIFY` step that the agent cannot skip. **All three paths must run.** See "Verification Mandate" section.

### Persona relay must be running before spawning
If `scripts/persona_relay.py` is not running on port 11435, every teammate's first `POST /chat/<persona>` will fail. Pre-flight: `curl -s http://localhost:11435/health` should return `{"ok": true}`. The relay is stdlib Python, no deps; start it as a background process at the top of the run.

### Ollama model registration must precede spawning
If `ollama list` doesn't show `<persona>-persona` entries for every persona, the relay's calls to Ollama will fail with "model not found". Pre-flight: the run's modelfile builder should report N/N OK before you run any team.

## Common Failure Patterns

| Pattern | What It Means | Skill Fix |
|---------|--------------|-----------|
| Agent writes TF from scratch, misses edge case | Skill doesn't mandate template usage | Add mandatory template fetch step |
| Same error across multiple personas | Core skill gap, not persona-specific | Add to gotchas/error handling table |
| Expert persona causes 3x more tool calls | Skill's advanced paths are underdocumented | Add enterprise/compliance section |
| Agent picks wrong cloud file | Skill's cloud routing is unclear | Improve the "read the cloud file" instruction |
| Agent hits error, can't self-correct | Error not in skill's error handling table | Add error + fix to the table |
| Verification test fails | Skill's verification workflow is incomplete | Fix the test parameters |
| Auth fails on every persona (AWS) | Env vars override provider config | Add explicit `env -u` step to auth check |
| S3 validation fails on workspace creation (AWS) | Missing bucket policy for E2 account | Add bucket policy to S3 setup gotcha |
| Classic cluster can't read UC tables | Missing data_security_mode | Add to verification workflow spec |
| Agent can't get workspace-level token | U2M/M2M mismatch on AWS | Document workspace auth pattern per cloud |
| Agent respawns hit same errors | Lessons not propagated | Bake known fixes into respawn prompts |
| Teammate dies without reporting | Agent hit context limit or crashed | Check for artifacts, respawn with smaller prompt |

## Tips

- **Start with 3-5 personas** spanning Low/Medium/High. Don't write 10 on day one.
- **The expert persona finds the most bugs.** Always include one.
- **Llama 3.2 3B is good enough** for roleplay. Cheap, fast, willing. Use it as the base model for all `*-persona` Modelfiles.
- **Real infrastructure is the point.** Pay for the cloud resources — the skill improvements pay for themselves.
- **Record everything.** Transcripts are the evidence. Plus `verification.json` per persona for structured pass/fail.
- **One loop iteration finds 5-15 improvements.** After 2-3 iterations, skills stabilize.
- **Always run the relay + Modelfiles for any new run.** Don't skip the persona-isolation step "to save time" — it leaks the test.
- **Ground new personas in real customer asks.** ASQs (SFDC), real Slack threads, real ES tickets, real RFP questions all make better personas than synthetic ones. The recommended pipeline is: gather a corpus of real asks, bucket by cloud × complexity × use-case, synthesize one composite persona per bucket.
