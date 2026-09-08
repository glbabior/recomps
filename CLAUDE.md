# Working on REComps

Read this before changing anything. The README explains what the project is and
why; this covers what a session needs to not break it.

## The two repositories

```
code/REComps/
  recomps/            this repo -- the engine
  <market>/           a private market plugin, cloned beside it
```

They are separate git repos, cloned side by side. The private one is used
straight from the folder — nothing is installed from it.

**The engine repo is currently private but is intended to be public.** The
owner wants to read the README before flipping it. Treat everything in it as
if it were already public: no real address, street name, agent name, postal
code, or listing datum, ever, including in tests, comments and commit
messages.

## Before you push anything public

```bash
cd ../<the private market repo> && ../recomps/.venv/Scripts/python -m pytest tests/ -q
```

That runs the denylist scan the public repo cannot hold — it builds a list of
every real address and agent name from the private fixtures and greps the
public checkout for them. It has caught real leaks three times, twice in
documentation where a war story kept its real street name.

The public repo has its own structural checks (`tests/test_privacy.py`), but
they can only assert that things *look* synthetic. Both halves matter.

## Setup

```bash
cd recomps
uv venv --python 3.13 && uv pip install -e ".[dev,live,ui]"
pytest                                    # 220 tests, all offline
recomps                                   # opens the interface
recomps run --market demoville --offline  # the whole pipeline, free, instant
```

The real market:

```bash
recomps run --market-path ../<the private market repo> --profile empty-lots \
  --offline --as-of 2026-08-04
```

That must reproduce these exactly. **If any of them moves, something is
broken** — they came from a real hand-run analysis and are pinned in
that repo's `tests/test_golden.py`:

```
sold 46, median $79.26/sqft · active 32, median $63.66/sqft
valuation $531,057 from 19 similar-size comps at $82.33/sqft
suggested list $499,000 · floor $486,000
```

## Rules that are not negotiable

These are commitments the project makes in writing. Breaking one is a defect
even when the code works.

**Never round before multiplying.** Every valuation multiplies an unrounded
$/sqft by a size. Rounding the rate to its displayed two decimals first moves
the answer by tens of dollars and silently breaks the golden numbers.

**No Excel formula may name a column letter.** Columns resolve through
`workbook/schema.py`, because a comp profile reshapes the sheet. A literal
`=C2/D2` anywhere is a bug waiting for the first non-land run.

**"Not found" is a value.** A missing agent, price or size stays `None`,
renders as an em dash, and is counted. Nothing is ever filled with a default,
an average, or a plausible guess.

**Fetching stays on this machine.** Rate limiting, `robots.txt` and honest
identification can only be *enforced* where the requests are made. Never spoof
headers, rotate user agents, imitate a browser, or retry a refusal differently.
A block is an answer.

**Fail soft, never fail silent.** A source that errors must land in the run
diagnostics. A caught exception that leaves no trace produces a thin run that
reads like a real one.

**Not an appraisal.** The owner is not an appraiser. Every artifact says so.

## Things that will bite you

**An `ANTHROPIC_API_KEY` in the environment shadows a Claude subscription.**
The owner is on a Max plan; the key was silently billing an API account
instead. `research/claude_code.py` strips it from the child process. Do not
"simplify" that away.

**A trailing `#n` means opposite things by property type.** On a condo it is
the unit — its identity. On a parcel it is an MLS lot number, and the same sale
appears both with and without it. Identity is profile-aware; see
`Identification.unit_suffix_is_significant`.

**Match sales on price, not date.** Recording date and close of escrow differ
by a day or two routinely, and one observed source was 18 days out. Price is
the primary key; dates are a tolerance.

**Read a page's embedded JSON before asking a model to read the page.** Most
listing sites ship their full result set as JSON in the HTML. Reading it is
exact, free and complete; a text-reading fetcher sees only what was
pre-rendered. This is why one index page yields 40 sales rather than 7.

**MLS filler reads like a name.** "Out Of Area Out Of Area" was returned as a
buyer's agent. `research/verify.clean_name` rejects placeholders.

## Where the work stopped

Done and working: the whole deterministic analysis, the workbook, snapshots and
run history, reopening a past run, live research end to end, both the CLI and
the Streamlit interface.

Open, roughly in order of value:

1. **Active listings.** The source that carried them now refuses this tool
   (HTTP 403, including for its own `robots.txt`). This costs the asking-price
   valuation basis and the best agent attribution. Needs a different source, or
   a browser session the owner drives. Do not attempt to evade the block.
2. **Validate the improved-property profiles.** Houses and condos are wired
   through everything and tested structurally, but no real run has checked
   their identification heuristics. They ship marked experimental.
3. **A map in the interface.** Quadrants are geographic and a misplaced parcel
   is obvious on a map and invisible in a table. Coordinates already come free
   from the index.
4. **Publish the engine repo**, once the owner has read the README.

## Reviewing

Three agents in `.claude/agents/`, written around this project's failure modes:
`code-reviewer` (leads with plausible-wrong-numbers over crashes),
`security-reviewer` (treats leaking into the public repo as top severity), and
`doc-writer` (verifies every claim against the code before writing it).

## House style

Match the surrounding code. Module docstrings carry the *reasoning* — what
failure this prevents, what the non-obvious constraint is — not a restatement
of the signatures. Comments explain why, never what. `ruff` decides formatting;
line length is 100.

Tests are named for the behaviour they protect, and several carry a one-line
docstring saying which real failure they exist to catch. Keep that.

Commit messages describe what the codebase now does differently, never the
instruction that produced them. "Describe the on-screen report before the
workbook", not "Lead with the on-screen report". The imperative mood is right
— "Fix CI", "Declare pydantic" — because the message completes *"applied, this
commit will…"*; naming the request instead of the change is what to avoid, along
with empty verbs like "enhance". The log is permanent and public, and it is what
a stranger reads first.
