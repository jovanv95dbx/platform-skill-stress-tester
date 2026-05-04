#!/usr/bin/env python3
"""Build per-persona Ollama models from a personas markdown file.

Reads a personas file (markdown with `## Persona NN: <name>` headers, each
followed by the FIRST fenced code block containing the persona's system prompt),
extracts each prompt, writes a Modelfile_<slug>, and runs:

    ollama create <slug>-persona -f Modelfile_<slug>

The persona system prompt is BAKED INTO the model — the SA agent never sees it.
This is layer 1 of persona isolation (see SKILL.md "Persona Isolation").

Usage:
    python3 templates/build_modelfiles.py --personas personas/my-run-personas.md
    python3 templates/build_modelfiles.py --personas personas/my-run-personas.md --base-model llama3.2:3b --out-dir personas/my-run-corpus

Notes:
    - Slug is derived from the first word after "## Persona NN: " (lowercased).
      e.g. "## Persona 01: Alice at Acme Corp" → slug = "alice"
    - Each Ollama model is named "<slug>-persona" — the persona relay (port 11435)
      expects this naming convention when routing /chat/<slug> requests.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SLUG_RE = re.compile(r"^## Persona \d+:\s*([A-Za-z]+)", re.MULTILINE)


def parse_personas(text: str) -> list[tuple[str, str]]:
    """Return [(slug, system_prompt), ...]. First fenced code block per section."""
    headers = list(SLUG_RE.finditer(text))
    out = []
    for i, m in enumerate(headers):
        slug = m.group(1).lower()
        section_start = m.end()
        section_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        section = text[section_start:section_end]
        cb = re.search(r"```(?:\w+)?\n(.*?)```", section, re.DOTALL)
        if not cb:
            print(f"  ! no code block found for {slug} — skipping", file=sys.stderr)
            continue
        out.append((slug, cb.group(1).rstrip()))
    return out


def build(slug: str, prompt: str, *, base_model: str, out_dir: Path) -> bool:
    modelfile_path = out_dir / f"Modelfile_{slug}"
    body = f'''FROM {base_model}
PARAMETER temperature 0.7
PARAMETER num_ctx 8192
SYSTEM """{prompt}"""
'''
    modelfile_path.write_text(body, encoding="utf-8")
    model_name = f"{slug}-persona"
    print(f"  building {model_name} ...", end=" ", flush=True)
    try:
        subprocess.run(
            ["ollama", "create", model_name, "-f", str(modelfile_path)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"FAILED\n    stdout: {e.stdout}\n    stderr: {e.stderr}", file=sys.stderr)
        return False
    print("ok")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--personas", required=True, type=Path, help="Path to the personas .md file")
    p.add_argument("--base-model", default="llama3.2:3b", help="Ollama base model (default: llama3.2:3b)")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="Where to write Modelfile_* (default: alongside the personas file in <name>-corpus/)")
    args = p.parse_args()

    if not args.personas.exists():
        print(f"persona file not found: {args.personas}", file=sys.stderr)
        return 1

    out_dir = args.out_dir or args.personas.with_suffix("").with_name(args.personas.stem + "-corpus")
    out_dir.mkdir(parents=True, exist_ok=True)

    personas = parse_personas(args.personas.read_text(encoding="utf-8"))
    if not personas:
        print("no personas parsed — file must have `## Persona NN:` headers + code blocks", file=sys.stderr)
        return 1

    print(f"found {len(personas)} personas: {', '.join(s for s, _ in personas)}")
    print(f"writing Modelfiles to {out_dir}")
    okct = sum(1 for s, p in personas if build(s, p, base_model=args.base_model, out_dir=out_dir))
    print(f"\n{okct}/{len(personas)} models built")
    return 0 if okct == len(personas) else 2


if __name__ == "__main__":
    sys.exit(main())
