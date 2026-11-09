# Demo script (about 4 minutes)

Setup: `python scripts/seed_demo.py --n 12 && streamlit run streamlit_app.py`. Everything shown is fictional.

**1. The problem (30 s)** — Inbox. "Each row is a commercial submission: an application, a liability supplement,
loss runs, payroll and vehicle schedules, financials and emails. Someone normally reads all of it, keys it in, and
hunts for what doesn't add up. PacketPilot does the reading and the hunting; a person still makes every call."

**2. Upload (30 s)** — *New submission package* → drop all files from `benchmark/packages/PKG-003`. Processing takes
well under a second. Point at the document list: each file was classified, with the signals shown.

**3. Evidence (45 s)** — Review → Fields → *All*. Open *FEIN*: the value, the exact source line, page and method.
Toggle *Show on page*: the line is highlighted on the original PDF. "Every value is traceable; nothing is taken on
faith."

**4. A contradiction (60 s)** — Back to *Needs attention*: *Annual revenue — Conflict*. The application and
supplement say $7.86M; the client's email says $13.76M. *Mark ready* is refused and the blockers are listed. Click
*Use this* on the application value — the conflict issue closes with the reviewer's name on it. "The model proposes,
rules validate, a human decides."

**5. The rest of the issues (45 s)** — Issues tab: a missing effective date, and a statement-of-values location
that isn't on the application. Follow-up email tab: the client email was written from those issues, in plain
language, without internal file names. *Request info* sets the status to awaiting info.

**6. Bulk approval and audit (30 s)** — *Approve N verified fields*: only fields that are high-confidence,
grounded, digitally sourced and uncontested. Audit trail: every action with who, when, before/after and model
version.

**7. Operations (30 s)** — Outstanding work, incomplete packages, SLA breaches, time to ready, first-pass ready
rate, and the correction rate by extraction method — the live signal of whether extraction can be trusted.

**Close (15 s)** — "On 48 fictional packages it got 99.9% of fields right on familiar layouts and caught every
injected problem; on an unfamiliar layout it read far less but marked nothing wrong as verified. Next step is
the LLM benchmark and a timed study with brokerage staff."
