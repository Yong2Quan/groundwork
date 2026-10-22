# When RAG is enough, and when you need an agent

A one-page decision guide, grounded in the measurements in
[README.md](README.md). The numbers come from a synthetic corpus, so treat
the *shape* of the finding as transferable and the absolute values as not.

---

## The one-sentence version

**Retrieval answers questions whose answer is written down somewhere. An
agent is what you need when the answer has to be assembled from live state
that nobody has written down yet** - and you pay several times the latency
and cost for that, so it is worth knowing which kind of question you have
before you build.

---

## Classify the question first

Before choosing an architecture, take twenty real examples of the task and
sort them into three buckets. This takes an afternoon and determines
everything downstream.

| Bucket | The answer is... | Architecture |
| --- | --- | --- |
| **1. Stated** | In the input itself | No retrieval. A single call. |
| **2. Written** | In a document you already have | RAG |
| **3. Assembled** | Only derivable by combining live state across sources | Agent |

The mistake that wastes the most money is building for bucket 3 when your
traffic is bucket 2. The mistake that damages trust fastest is shipping
bucket 2 when your traffic is bucket 3, because the system will answer
confidently and be wrong.

### Telling bucket 2 from bucket 3

The test is a counterfactual: **could a well-informed expert answer this
correctly with the documentation open in front of them and nothing else?**

- Yes -> bucket 2. Retrieval is enough.
- No, they would have to go and look something up -> bucket 3. You need
  tools.

In this corpus that distinction is exactly the `runbook_sufficient` /
`evidence_required` split. The runbook tells you *how* to distinguish
connection-pool exhaustion from a slow query - look at whether query time
moved - but it does not and cannot contain this morning's numbers. Perfect
retrieval of the perfect document still leaves you unable to answer.

---

## What the measurements showed

Thirty synthetic incidents, three configurations, Claude Sonnet 5.5. Full
tables in [README.md](README.md#results).

Sonnet 5.5, with the Haiku 5.5 figures in brackets where they differ.

| | LLM alone | RAG | Agent |
| --- | ---: | ---: | ---: |
| Ticket is enough (bucket 1) | 100% | 100% | 100% |
| Needs a runbook (bucket 2) | 82% | 91% | 100% (91%) |
| Needs live evidence (bucket 3) | **9%** | **9%** | **100% (100%)** |
| Cost per incident | $0.0087 | $0.0130 | $0.0618 ($0.0047) |
| Median latency | 3.5s | 4.1s | 18.6s |

**Five things to take from this, in order of how much they should change
what you build.**

**1. On bucket 3, retrieval was worth nothing at all.** Not "less" - the
same 9% as no retrieval. The agent got 100%. This is the entire argument
for tools: when the answer is in state nobody has written down, no document
retrieves it. If your traffic is bucket 3, retrieval is not a cheaper
approximation of an agent. It is a different thing that does not work.

**2. On bucket 2, RAG bought one incident out of thirty.** It retrieved the
right runbook 91% of the time and still added almost nothing, because the
model already knew the content. The agent called `search_runbooks` **twice
in thirty incidents** - it wanted data, not procedure.

So: *do not build RAG over documentation that encodes what a competent
practitioner already knows.* Retrieve what the model genuinely cannot know -
your topology, your conventions, your team's decisions, last quarter's
incident. General best practice is already in the weights.

**3. The cheaper arms failed safely, which changes the recommendation.**
Across all 90 runs, **no configuration gave a single confident wrong
answer**. The LLM-alone and RAG arms abstained - 40% and 37% of the time -
and they abstained on the incidents they genuinely could not solve. The gap
is in *resolution*, not correctness.

A system that says "I cannot tell from this" is routable to a human and is
safe. One that guesses is neither.

The control arm makes this concrete. A BM25 keyword matcher with no model
at all scores **63% - exactly the same as RAG** - and on the hardest tier
it appears to beat both LLM arms, 36% against 9%. On accuracy alone you
would ship it: it is free and instant.

It is the worst option available. It reaches 63% by guessing on every
incident and is confidently wrong 11 times out of 30. Priced with
`triage economics`, it costs **$2,062 per 100 incidents against $6.18 for
the agent** - the free system is orders of magnitude more expensive than
the one that calls an LLM on every incident, because misdirecting an
engineer costs more than any amount of inference. (That multiple is
illustrative: it assumes a misdirection costs 45 engineer-minutes. Halve
the assumption and you halve the figure - but the control stays the more
expensive of the two at every setting.)

Two systems, identical accuracy, opposite verdicts. **If your eval has no
abstain option and no cost model, you cannot tell them apart** - and the
cheap one will win your bake-off.

**4. Access beat model size, and it was not close.** The whole sweep was
repeated on Claude Haiku 5.5. On `evidence_required` both models scored
identically at every configuration - including **11/11 for both agents**.
Dropping to the cheaper model cost 7 points on the headline; removing tool
access cost 40.

Which produces the comparison that should decide your build:

| | Accuracy | Cost / incident |
| --- | ---: | ---: |
| **Haiku agent** | **93%** | **$0.0047** |
| Sonnet RAG | 63% | $0.0130 |

The cheap model with tools beat the expensive model without them by 30
points *and* cost 2.8x less. **If you are choosing between a better model
and better access to data, buy the access.** Reach for the frontier model
only once the system can already see what it needs to.

**5. The agent cost 7x the LLM alone and 5x the latency.** $0.0618 vs
$0.0087, 18.6s vs 3.5s. On bucket 1 and 2 it bought nothing you could not
get for a seventh of the price. On bucket 3 it was the only thing that
worked. That is the whole decision, in one line: **pay the 7x only where
the evidence is live.**

### The caveat that matters most

The agent scored 100%, which means this corpus is saturated: it can no
longer distinguish a good agent from a great one, and every number above is
a floor rather than a measurement. The evidence here is also cleaner than
reality - one metric window per incident, no unrelated concurrent
anomalies. Expect the bucket-3 gap to remain large and the agent's absolute
score to fall on real data.

---

## Rules of thumb

**Use a single call when** the input contains the answer. Adding retrieval to
a bucket-1 task adds latency and a new failure mode (retrieving something
irrelevant and being misled by it) for no accuracy.

**Use RAG when** your answers live in documents that change slower than your
questions arrive. It is dramatically cheaper and faster than an agent, it is
easy to debug - you can look at what was retrieved - and it fails in
legible ways.

**Use an agent when** at least one of these is true:

- The answer depends on state that changes faster than you can index it
  (metrics, queue depth, current config, who is on call).
- The question requires *combining* two or more sources, where which sources
  you need depends on what the first one said.
- The work is genuinely multi-step and the steps are not knowable in advance.

**Do not use an agent because** the task feels important, because the input
is long, or because you want the system to seem capable. None of those are
the criterion.

---

## If you do build the agent

**Measure retrieval separately.** When a RAG answer is wrong, you need to
know whether the right document was retrieved. Record what was retrieved on
every call. Otherwise you will tune the prompt when the problem is the
index.

**Make tools refuse rather than guess.** The worst defect found while
building this was a metrics tool that, given an ambiguous time window,
returned *a* window instead of erroring. It produced confident answers over
the wrong evidence and no error anywhere. Tools that guess convert a loud
failure into a silent one.

**Do not let tools do the reasoning.** A `get_metrics` that helpfully
returned "the series that moved most" would score far better and teach you
nothing about whether the model can diagnose. If your eval improves when you
make the tool smarter, you are measuring the tool.

**Budget the steps and report abstention.** Give the loop a hard step
ceiling, and give the model an explicit `unknown` option. A system that says
"I could not tell" is routable to a human; one that always guesses is not,
and its accuracy number hides the difference.

**Expect the cost multiple.** An agent's cost is roughly (steps x context
growth) relative to a single call. Know that multiple before you commit, and
measure it on your own traffic rather than trusting a vendor benchmark.

---

## The hybrid that usually wins

Most production systems should not pick one. The measurements here point at
a specific routing design, and it is cheaper than it looks:

1. **Try the cheap arm first.** RAG, or even the model alone.
2. **Route on abstention, not on a confidence threshold.** This is the
   useful part. The cheap arms abstained on 37-40% of incidents and were
   *never confidently wrong* - their `unknown` was an almost perfect
   detector for "this one needs evidence". You do not have to predict which
   bucket an incident falls into in advance. You ask, and the cheap arm
   tells you when it cannot answer.
3. **Escalate only those to the agent.** On this corpus that means paying
   the 7x cost on roughly 40% of traffic instead of all of it, with no
   accuracy lost, because the agent solved everything the cheap arms
   abstained on.

Costed out with `triage economics`, per 100 incidents: all-agent $6.18,
LLM-alone-then-agent **$3.80 - 39% cheaper, same 100% resolution.** That
holds at every escalation cost from 5 to 90 engineer-minutes, because both
options resolve everything and so never pay the escalation term at all.

Note which cheap arm wins. `LLM alone -> agent` beats `RAG -> agent`
($3.80 vs $4.07): RAG costs more per incident and resolves exactly one
more incident than the model alone - 19 against 18, since the 3-point gap
between them is percentage points, not incidents - so it hands off nearly
as much work while charging more to do it. **If you are going to escalate anyway, the pre-filter should
be the cheapest thing that abstains honestly** - not the most capable thing
short of an agent.

Two conditions make this work, and both are worth checking before you rely
on it:

- **The answer set must include an explicit `unknown`,** and your prompt
  must say that abstaining is acceptable. Without it the cheap arm guesses
  and the routing signal disappears.
- **Abstention must actually track difficulty.** Verify it: a system that
  abstains at random is just a coin toss that costs you an escalation.
