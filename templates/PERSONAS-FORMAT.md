# Persona file format

`build_modelfiles.py` and `build_spawn_prompts.py` parse a markdown file with the following format. Personas themselves are domain-specific and out of scope for this repo — write your own based on real customer asks for the skill set you're testing.

## Format spec

```markdown
## Persona 01: <FirstName> — <short description>

Optional human-readable preamble for you (the test designer). Anything outside
the fenced code block is ignored by the parser. Use it for notes about what
this persona tests or why it exists.

​```
You are role-playing as <FirstName> <LastName>, a <role> at <company>.

<everything that defines the persona — situation, technical context,
behaviour rules, hard rules. This entire code block is what becomes
the customer's hidden system prompt baked into the Ollama model.
The SA agent NEVER sees this content.>
​```

---

## Persona 02: <FirstName> — <short description>

​```
<another persona's system prompt>
​```
```

## Rules

1. **Header line:** `## Persona NN: <FirstName>` — the first word after `## Persona NN:` becomes the slug (lowercased).
   - `## Persona 01: Alice at Acme` → slug `alice` → Ollama model `alice-persona` → relay endpoint `POST /chat/alice`
2. **Body:** the FIRST fenced code block in each persona's section is the persona's system prompt. Anything else in the section is ignored.
3. **Slugs must be unique within a file.** Don't have two `Alice` personas; pick distinct first names.
4. **Each code block is the entire customer definition.** Treat it as if it were a system prompt: situation, knowledge level, behaviour rules ("when asked about X, say Y"), hard rules ("DO NOT accept Z").

## Where to put real persona files

By convention, `personas/<run-name>-personas.md`. The `.gitignore` excludes `personas/*-personas.md` and `personas/*-corpus/` so real persona files (which often contain real customer names from Salesforce ASQs, etc.) stay local.
