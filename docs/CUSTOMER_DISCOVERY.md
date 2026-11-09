# Customer discovery kit

**Status: no interviews have been conducted yet.** This is the kit for running them. Don't cite findings that don't
exist; when interviews are done, add a dated summary at the bottom with counts, not adjectives.

## Who to talk to (target 8–10)

| Role | Why | Where to find them |
|---|---|---|
| Account manager / CSR at an independent agency (5–50 staff) | does the intake and the chasing | LinkedIn search "commercial lines account manager", state independent-agent associations, local agency networking events |
| Submission / placement specialist at a wholesaler or MGA | receives packets from many agencies, sees every format | LinkedIn, insurance Slack/Discord communities |
| Agency operations manager / principal | owns turnaround and staffing | agency association events, warm intros |
| Small-commercial underwriter (1–2 only) | knows what makes a submission "complete" | LinkedIn; ask for 20 minutes on "what makes you decline to quote" |

Screener (send before booking): Do you personally handle new-business commercial submissions? Roughly how many per
week? Which lines? What system do they end up in? If "none" or "I only manage people who do", note it and decide
whether to keep the slot.

## Ground rules

* 30 minutes, video or phone, recorded only with consent. Ask about the **last real submission**, not opinions about
  the future. Don't demo until the last 10 minutes, and only if the problem showed up.
* Never ask for real client documents. If they want to show something, ask them to describe it or redact it on
  their side first.
* Mention that you're building an independent tool and not representing any employer or carrier.

## Script

**Warm-up (3 min)**
1. What's your role, and what share of your week is new commercial business?
2. Which lines and industries do most of your submissions involve?

**The last submission (12 min)** — "Walk me through the most recent new-business submission you worked on."
3. How did it arrive? (email, portal, AMS, paper) How many files, what kinds?
4. What did you do first? Where did the information go?
5. What was missing or didn't add up? How did you notice?
6. How did you follow up with the client? How many rounds? How long did it take to hear back?
7. Roughly how long did you spend on it, start to finish, excluding waiting time?
8. When was it "good enough" to send to markets? Who decided?

**Patterns (8 min)**
9. Which items do you chase most often? (loss runs, payroll by class, vehicle lists, explanations…)
10. Tell me about a time something wrong got past intake. What did it cost?
11. What tools do you use today for this? What have you tried and dropped? Why?
12. If this part of the job took half as long, what would you do with the time?

**Prototype reaction (7 min, only if relevant)** — show the review screen with a fictional package.
13. What would you need to see before trusting a pre-filled value?
14. What's missing for this to fit your process? What would stop you from using it?
15. Who else would need to agree before your agency tried something like this?

**Close**: Can I follow up when there's a version to try with fictional data? Who else should I talk to?

## Note template (one per interview)

```
Date / interviewee role / agency size / lines
Last submission: arrival channel, # files, file types
Time spent (their estimate): ___ min   Follow-up rounds: ___
Missing/contradictory items they hit (verbatim):
Tools in use:
Biggest pain (verbatim quote):
Trust requirements for pre-filled data:
Would try a prototype? (yes / maybe / no) Why:
Referrals:
```

## Synthesis

After 5 and again after 10 interviews, tally:

| Question | How to summarise |
|---|---|
| Is intake a top-3 time sink? | count yes / no / unclear |
| Most-chased items | frequency table of items mentioned unprompted |
| Self-reported minutes per submission | median and range (state that it's self-reported) |
| Follow-up rounds | median and range |
| Trust requirements | group verbatim quotes into themes |
| Blockers to adoption | AMS integration, security review, cost, habit… with counts |

Decision rule worth writing down in advance: if fewer than half say intake is a top-3 time sink, or the most-chased
items aren't things PacketPilot detects, change direction before building more.

## Prototype test (2–3 people)

Use `benchmark/packages` (fictional). Give each person 3 packages, ask them to get each to "ready" or "request
info" while thinking aloud, then run the timed protocol in [EVALUATION.md](EVALUATION.md#review-time-study-protocol-to-run-with-real-people).
Record: where they hesitated, what they checked on the page image, which issues they disagreed with, and every
time they wanted something the tool didn't do.

## Findings log

_None yet._
