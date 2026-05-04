#!/usr/bin/env python3
"""Build per-teammate spawn-prompt briefing files from a personas markdown file.

For each persona (parsed from the personas .md), writes a briefing .txt under
<corpus-dir>/spawn-prompts/<slug>-spawn.txt that the SA agent reads as its
opening instructions. The briefing contains:
  - Cloud auth instructions
  - Skills directory path (read all skill files, no filtering)
  - Relay endpoint for talking to the customer
  - 3-path verification mandate
  - Activity-logging mandate
  - Lessons-learned (auth gotchas)
  - Step-by-step workflow

The briefing does NOT contain the persona system prompt — that lives baked
into the Ollama model (see build_modelfiles.py + SKILL.md "Persona Isolation").

Cloud detection: the script reads the prefix line of each persona's first code
block. If it contains "AWS" the persona is treated as AWS; if "Azure" then Azure;
otherwise the user passes --cloud per persona via CLI.

Usage:
    python3 templates/build_spawn_prompts.py \\
        --personas personas/my-run-personas.md \\
        --skills /path/to/ai-platform-kit/.claude/skills \\
        --run-name my-run

The script writes to:
    personas/<basename>-corpus/spawn-prompts/<slug>-spawn.txt   # one per persona
    personas/<basename>-corpus/run_index.json                   # registry of paths
    deployments/<run-name>/<slug>/                              # empty TF dirs (auto-created)
    transcripts/<run-name>/                                     # empty (auto-created)
    logs/<run-name>/                                            # empty (auto-created)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PERSONA_HEADER_RE = re.compile(
    r"^## Persona \d+:\s*([A-Za-z]+)([^\n]*)$",  # group1=first-name slug, group2=rest of header
    re.MULTILINE,
)


def detect_cloud(header_rest: str, body: str) -> str:
    """Best-effort cloud detection from the persona header/body."""
    txt = (header_rest + "\n" + body).lower()
    if "(aws" in txt or " aws " in txt or "amazon web services" in txt:
        return "AWS"
    if "(azure" in txt or " azure " in txt or "microsoft azure" in txt:
        return "Azure"
    if "(gcp" in txt or " gcp " in txt or "google cloud" in txt:
        return "GCP"
    return "unknown"


def parse_personas(text: str) -> list[dict]:
    """Return [{slug, full_name, cloud, header_rest}, ...]"""
    headers = list(PERSONA_HEADER_RE.finditer(text))
    out = []
    for i, m in enumerate(headers):
        slug = m.group(1).lower()
        rest = m.group(2).strip()
        section_start = m.end()
        section_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[section_start:section_end]
        out.append({
            "slug": slug,
            "full_name": (m.group(1) + rest).strip(),
            "header_rest": rest,
            "cloud": detect_cloud(rest, body),
        })
    return out


AWS_AUTH_BLOCK = """\
==============================
TARGET CLOUD + AUTH (AWS — CRITICAL)
==============================
Cloud: AWS

Set on every shell call:
  export AWS_PROFILE=<your-aws-profile>
  export AWS_DEFAULT_REGION=<region from conversation>

Verify before doing anything:
  aws sts get-caller-identity

Databricks auth — CANONICAL SRA PATTERN (no per-workspace browser login):
The team lead has pre-created an Account-Admin service principal for this run.
Credentials are at: /tmp/canonical-sp/creds.json (mode 600).

Source them once at the top of your shell session:
  source /tmp/canonical-sp/sourceme   # exports DATABRICKS_CLIENT_ID/SECRET/ACCOUNT_ID

Or pass per-command:
  env -u DATABRICKS_HOST \\
      DATABRICKS_CLIENT_ID="..." DATABRICKS_CLIENT_SECRET="..." DATABRICKS_ACCOUNT_ID="..." \\
      AWS_PROFILE=... AWS_DEFAULT_REGION=... \\
      terraform apply

Both the account-level and workspace-level Databricks providers authenticate
with the SAME env-var creds. Account Admin SPs implicitly resolve workspace
admin against any workspace. NO per-workspace browser login. NO PAT. NO
per-persona SP creation. See platform-provisioning/AWS.md "Authentication"
for the full pattern.

Known auth gotchas:
- Stale DATABRICKS_HOST in your shell silently overrides provider host.
  Use `env -u DATABRICKS_HOST ...` on every command.
- AWS sandbox accounts are often ENTERPRISE-only — adjust pricing tier if
  the skill defaults to PREMIUM and apply rejects.
- For UC IAM role: include sts:AssumeRole self-assume in INLINE policy too,
  not just trust policy (UC validates by self-assume).
"""

AZURE_AUTH_BLOCK = """\
==============================
TARGET CLOUD + AUTH (Azure — CRITICAL)
==============================
Cloud: Azure

Verify before doing anything:
  az account show   # confirm subscription + tenant match the customer's

Databricks Azure account-level auth:
  Use `auth_type = "azure-cli"` in the Terraform databricks provider so it
  picks up your `az login` creds. Set `azure_tenant_id` on EVERY databricks
  provider block — tenant mismatch is the #1 Azure auth failure.

Known auth gotchas:
- Stale DATABRICKS_HOST/DATABRICKS_CLIENT_ID/DATABRICKS_CLIENT_SECRET/DATABRICKS_ACCOUNT_ID
  in your shell silently override `auth_type = "azure-cli"`. Use
  `env -u DATABRICKS_HOST -u DATABRICKS_CLIENT_ID ... terraform apply`.
- `databricks_mws_permission_assignment` does NOT work on Azure (AWS/GCP only).
  Account-level groups auto-sync via UC identity federation.
- `tier=PREMIUM` from the customer maps to `sku="premium"` on
  azurerm_databricks_workspace. Enterprise tier is account-level licensing,
  not a Terraform sku value.
"""

TEMPLATE = """\
You are {full_name}, a Databricks Solutions Architect. You're running a stress
test of the platform-kit skills. Your job: have a NATURAL conversation with a
simulated customer (played by an Ollama model via the persona relay), then
deploy REAL infrastructure based on what they need, then run the mandatory
3-path verification, then write a transcript + summary.

YOU ARE THE SA. You do NOT know who the customer is, what cloud they want, or
what they need until they tell you. Discover via conversation.

Run name: {run_name}
Persona slug: {slug}
Output dirs (auto-created):
  Deployment dir : {deployment_dir}
  Transcript file: {transcript_file}
  Logs dir       : {logs_dir}

{auth_block}

==============================
SKILLS DIRECTORY — READ ALL FILES, NO FILTERING
==============================
Skills root: {skills_dir}

Read EVERY .md file under that directory recursively. ALL of them — Azure, AWS,
GCP, deployment-verification, identity-governance, platform-provisioning,
private-networking, unity-catalog-setup, workspace-config. Do not pre-filter
by cloud — you don't know the customer's cloud yet.

==============================
LOGGING MANDATE (CRITICAL — for post-run analysis)
==============================
The relay automatically writes the conversation to:
  {logs_dir}/{slug}-chat.jsonl

YOU also append milestone events to your activity log as you work:
  {logs_dir}/{slug}-activity.jsonl

Append one JSON line per event using this exact shape:
  {{"ts":"<UTC ISO8601>","persona":"{slug}","event":"<event>","data":<obj>}}

Required events (write at minimum these):
  - skills_read     : {{"count": N}}
  - customer_turn   : {{"turn": N, "summary": "..."}}
  - decision        : {{"what":"...","why":"..."}}
  - tool_run        : {{"cmd":"...","cwd":"...","exit":<int>}}
  - error           : {{"where":"...","msg":"...","fix":"..."}}
  - terraform_apply : {{"resources_added": N, "resources_changed": N, "resources_destroyed": N}}
  - verification    : {{"path":"classic|sqlwh|notebook","status":"PASS|FAIL|SKIPPED","duration_s":N,"detail":"..."}}
  - skill_gap       : {{"skill":"<dir>","what":"...","severity":"low|med|high"}}
  - done            : {{"status":"success|partial|failed","summary":"..."}}

Use `date -u +"%Y-%m-%dT%H:%M:%SZ"` for the timestamp. Append (do not overwrite).

Final summary: write {logs_dir}/{slug}-summary.json at end with verdict +
all skill gaps + observations.

==============================
RELAY (CUSTOMER CHAT — ONLY way to talk to the customer)
==============================
First message:
  curl -s -X POST http://localhost:11435/chat/{slug} \\
       -H 'Content-Type: application/json' -d '{{}}'
  -> returns {{"persona":"{slug}","turn":1,"customer":"..."}}

Subsequent turns:
  curl -s -X POST http://localhost:11435/chat/{slug} \\
       -H 'Content-Type: application/json' -d '{{"text":"<your SA reply>"}}'
  -> returns {{"persona":"{slug}","turn":N,"customer":"..."}}

Conversation length: 3-15 turns. Stop when the customer accepts the plan AND
you've deployed + verified, OR when you hit a hard blocker.

Do NOT inspect the persona definition file or any Modelfile_*. Do not run
`ollama show`. You only see what the customer says via the relay.

==============================
STEP-BY-STEP WORKFLOW
==============================
STEP 1 — Read every .md under {skills_dir} recursively.
STEP 2 — Open the conversation (POST /chat/{slug} with empty body).
STEP 3 — Have a natural conversation. Discover cloud, region, tier, networking,
         compliance, workspace name, catalog, tags, etc.
STEP 4 — Deploy. Real Terraform, real cloud account. Write all .tf to
         {deployment_dir}/terraform/. Don't deploy more than the customer asked.
STEP 4.5 — Verify (mandatory, all 3 paths). Read deployment-verification/SKILL.md
           and run all three compute paths against a UC table:
             1. Classic cluster (data_security_mode=SINGLE_USER, autotermination=0)
             2. Serverless SQL warehouse (PRO, 2X-Small) — NOTE: use 4 separate
                /api/2.0/sql/statements calls, NOT a multi-statement body
             3. Serverless notebook job (Python with dbutils.notebook.exit) —
                NOTE: extract tasks[0].run_id for runs/get-output
           Save verification.json + verification.log to {deployment_dir}.
STEP 5 — Write transcript to {transcript_file}.
STEP 6 — Write {logs_dir}/{slug}-summary.json with verdict + skill gaps.
STEP 7 — Mark task completed and report to team-lead.

==============================
HARD RULES
==============================
- ALL skills loaded — read them all. Do not pre-filter by cloud.
- Real Terraform, real cloud, no mocks.
- 3 verification paths, mandatory.
- Cloud quota errors during apply: STOP and escalate to customer (do not
  silently drop features to make the apply succeed).
- If you create UC objects (catalog, external location, storage credential)
  with an SP, the human admin group needs explicit `databricks_grants` —
  workspace admin alone won't show them in the UI.
- Persona isolation — DO NOT read any *.md or Modelfile_* under the corpus dir.
- ALWAYS append to your activity log.
- Do not commit to git or push anywhere. Local artifacts only.
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--personas", required=True, type=Path, help="Path to the personas .md file")
    p.add_argument("--skills", required=True, type=Path, help="Path to platform-kit .claude/skills/")
    p.add_argument("--run-name", required=True, help="Logical run name (used in output dir paths)")
    p.add_argument("--project-root", type=Path, default=Path.cwd(),
                   help="Project root for deployments/transcripts/logs (default: cwd)")
    args = p.parse_args()

    if not args.personas.exists():
        print(f"persona file not found: {args.personas}", file=sys.stderr)
        return 1
    if not args.skills.exists():
        print(f"skills dir not found: {args.skills}", file=sys.stderr)
        return 1

    text = args.personas.read_text(encoding="utf-8")
    personas = parse_personas(text)
    if not personas:
        print("no personas parsed — file must have `## Persona NN:` headers", file=sys.stderr)
        return 1

    corpus_dir = args.personas.with_name(args.personas.stem + "-corpus")
    spawn_dir = corpus_dir / "spawn-prompts"
    spawn_dir.mkdir(parents=True, exist_ok=True)

    deployments_root = args.project_root / "deployments" / args.run_name
    transcripts_root = args.project_root / "transcripts" / args.run_name
    logs_root = args.project_root / "logs" / args.run_name
    deployments_root.mkdir(parents=True, exist_ok=True)
    transcripts_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)

    written = []
    for p in personas:
        slug = p["slug"]
        deployment_dir = deployments_root / slug
        transcript_file = transcripts_root / f"{slug}.md"
        deployment_dir.mkdir(parents=True, exist_ok=True)
        (deployment_dir / "terraform").mkdir(parents=True, exist_ok=True)

        if p["cloud"] == "AWS":
            auth_block = AWS_AUTH_BLOCK
        elif p["cloud"] == "Azure":
            auth_block = AZURE_AUTH_BLOCK
        else:
            auth_block = "==============================\nTARGET CLOUD + AUTH\n==============================\nCloud unknown — discover from the conversation. See platform-provisioning/{AWS,AZURE,GCP}.md for cloud-specific auth instructions.\n"

        body = TEMPLATE.format(
            full_name=p["full_name"],
            run_name=args.run_name,
            slug=slug,
            deployment_dir=deployment_dir,
            transcript_file=transcript_file,
            logs_dir=logs_root,
            auth_block=auth_block,
            skills_dir=args.skills,
        )
        out = spawn_dir / f"{slug}-spawn.txt"
        out.write_text(body, encoding="utf-8")
        written.append(out)

    index = {
        "run": args.run_name,
        "personas_file": str(args.personas),
        "skills_dir": str(args.skills),
        "personas": [
            {
                "slug": p["slug"],
                "full_name": p["full_name"],
                "cloud": p["cloud"],
                "spawn_prompt": str(spawn_dir / f"{p['slug']}-spawn.txt"),
                "deployment_dir": str(deployments_root / p["slug"]),
                "transcript": str(transcripts_root / f"{p['slug']}.md"),
                "chat_log": str(logs_root / f"{p['slug']}-chat.jsonl"),
                "activity_log": str(logs_root / f"{p['slug']}-activity.jsonl"),
                "summary": str(logs_root / f"{p['slug']}-summary.json"),
            }
            for p in personas
        ],
    }
    (corpus_dir / "run_index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    print(f"wrote {len(written)} spawn briefings to {spawn_dir}/")
    for w in written:
        print(f"  {w.name}")
    print(f"index: {corpus_dir / 'run_index.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
