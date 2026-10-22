# Build log

Where the AI coding tool helped, where it was wrong, and what I had to
correct. Written as I went rather than reconstructed afterwards, so the
mistakes are the ones that actually happened.

The project was built alongside using Claude Code. I checked the output against reality by running it, and
the corrections below are the places where running it disagreed with the
draft.

---

## Where it helped

**Scaffolding and boilerplate.** The BM25 retriever, the provider interface,
the argparse CLI, the report renderer and the dataclasses were all drafted
faster than I would have typed them, and mostly correct first time.

**The eval design.** The strongest contribution was not code. The original
brief asked for 30 incidents and an accuracy comparison. The assistant
proposed splitting the corpus into three *answerability tiers* -
`ticket_sufficient`, `runbook_sufficient`, `evidence_required` - so that the
comparison would have an independent variable instead of one undifferentiated
accuracy number. That change is what makes the results interpretable: without
it, "the agent scored higher" tells you nothing about *when* to use an agent,
which is the actual question the playbook has to answer.

**Adversarial checks on its own data.** It wrote the leakage check that later
caught two flaws in the data it had itself written (below). Asking for a test
that could invalidate the work was more productive than asking for more work.

---

## Where it was wrong, and what I corrected

### 1. Stale knowledge of the MCP SDK (two separate breaks)

The first draft of the server used `from mcp.server.fastmcp import FastMCP`.
The installed SDK is 2.3.0, where that class is `MCPServer` in
`mcp.server.mcpserver`. The SDK itself caught this with a clear error.

The same stale prior bit again inside the agent loop: `tool.inputSchema`
(v1 camelCase) is `tool.input_schema` in v2. That one only surfaced at
runtime, mid-connection.

**Lesson:** the model's prior on a fast-moving SDK is a guess. Both breaks
were found by running the code, not by reading it. Neither would have been
caught by review.

### 2. A silent-wrong-answer bug in `get_metrics`

The first implementation of the service/window lookup ended with:

```python
if not candidates:
    raise KeyError(...)
return metrics_for(candidates[0])
```

`checkout-api` has five retained metric windows. If the agent called
`get_metrics` without a window - which it has every incentive to do, since
that is less work - it would get **a different incident's metrics**, with no
error, and reason confidently over the wrong evidence. The eval would have
scored that as a reasoning failure.

I replaced the fallback with an `AmbiguousWindow` error that refuses and
reports the retained windows. There is now a test pinning it.

**Lesson:** this is the failure mode worth hunting for in agent tooling. It
was not a crash, it was a plausible wrong answer, and the accuracy number
would have absorbed it silently.

### 3. The leakage check invented leaks twice before it worked

The validity check asks whether an `evidence_required` ticket gives away its
own answer to a keyword matcher. First version used Jaccard similarity and
flagged 4 of 11 incidents. Second version used coverage and flagged a
*different* 4.

Neither was measuring leakage. Jaccard divides by the union, so a cause
documented across two long runbooks is penalised; coverage divides by the
query, so the same cause is rewarded. Each measure was reporting corpus
structure as if it were a property of the ticket.

The fix was to require both measures to agree, which cancels the opposing
bias, and to compare against each incident's *designated distractors* rather
than all 14 causes - because "the ticket does not single out one cause" is a
claim about the candidates it was built to be confused with.

That version found exactly one genuine leak, and it was mine: I had written
"We have not changed our call pattern" into INC-027, which rules out
throttling outright. Removed. A second, milder tell ("latency has been
*climbing*", the slow-query signature) was softened in INC-021.

**Lesson:** I nearly accepted the first run of a metric I had just written.
The thing that saved it was looking at the margins rather than the pass/fail,
and noticing that near-ties were being reported as leaks.

### 4. The MCP server could not be attached the way I planned

The original design let Claude Code attach the MCP server and drive the tool
loop itself. On this machine that is blocked:

```
Warning: MCP server blocked by enterprise policy: triage
```

A remote organization policy disallows MCP servers. The first smoke test
still "passed" in the sense that it returned an answer - the model just
quietly reported it had no tools and guessed `unknown` from the ticket. Had I
only checked the exit code, I would have shipped an agent arm that never
called a tool.

The fix was to move the agent loop into the harness: we own the MCP client
(official Python SDK, stdio), list the tools, and drive the loop ourselves,
using the model purely as a completion endpoint. That turned out better than
the original plan - the loop is now provider-agnostic and every step is
recorded in the trace, which is what makes the failure analysis possible.

**Lesson:** "it returned a result" is not "it did the thing". Check that the
work happened, not that the call succeeded.

### 5. The eval found a labelling mistake I had made

Running the sweep surfaced something review would not have. `INC-016` was
labelled `runbook_sufficient`. Both non-agent arms abstained on it, and the
RAG arm explained why - after retrieving the correct TLS runbook at rank 1:

> "It does not separate an expired TLS certificate from a bad deploy, config
> drift or a DNS failure, because each of those [is also a step function]."

It was right and I was wrong. RB-002 - which I wrote - says the metric shape
is the giveaway, so the incident belongs in `evidence_required`.

I left the label alone and wrote it up instead. Correcting a tier after
seeing which configuration it favours is the data-fitting this whole project
is meant to avoid, and the honest version is a better artifact than a clean
11/11.

**Lesson:** the eval's job is to disagree with you. The temptation to "fix"
a label that makes your numbers worse is exactly the moment to stop.

### 6. A cheaper model found a defect neither I nor Sonnet caught

Repeating the sweep on Haiku 5.5 was meant to answer "is this the
architecture or the model". It did, but it also turned up something I had
not looked for. Haiku abstained on `INC-017` and said why:

> "restart_count reads 0 across the window, which contradicts the reported
> crash-looping"

The ticket says two replicas are crash-looping. The metric profile for
`queue_consumer_lag` never touches `restart_count`, so it sits at zero for
the whole window. My ticket text and my generated metrics contradict each
other, and that contradiction is part of why the model would not commit.

I wrote both halves and did not notice. Sonnet solved the incident without
remarking on it - which is arguably the worse outcome, since it got the
right answer over inconsistent evidence.

In the same verdict Haiku also claimed `find_similar_incidents` was
unavailable to it. That was false: the tool was in the catalogue and it
called it successfully twice elsewhere in the same sweep. So the trace
contained one true observation I had missed and one invented one, in the
same paragraph, at the same confident register.

**Lesson:** read the traces, not just the scores. Both findings were
invisible in the accuracy table, and only one of them was real - which is
why the tool log is the arbiter and the model's account of itself is not.

### 7. Smaller corrections

- `PROTOCOL.format(max_steps=...)` raised `KeyError: '"action"'` because the
  prompt template contains JSON braces. Swapped for an explicit placeholder.
- `claude -p --resume <id>` silently started a *new* session instead of
  resuming, which would have made every agent step a fresh conversation. I
  verified by checking the returned `session_id` against the one I passed,
  and abandoned session-resume in favour of an explicit transcript.
- The report rendered the rules-only control arm under the label "LLM
  alone". Harmless looking, actively misleading - it prints a keyword
  matcher's accuracy as a model result. Now labelled by provider.
- Several long shell heredocs were silently truncated by the tool, producing
  files that parsed but were incomplete. Caught by `ast.parse` and line
  counts after each write.

---

## What I would do differently

**Verify the environment before designing around it.** Two of the four real
problems above - the MCP policy block and the SDK version - were facts about
this machine that I could have established in the first five minutes. I
designed first and discovered second.

**Write the invalidating test before the thing it validates.** The leakage
check should have existed before the 30 incidents, not after. Writing it
second meant I was checking data I was already attached to, and my first
instinct on seeing it fail was to adjust the check.

**Distrust silent successes more than loud failures.** Every genuinely
dangerous defect here - the wrong metric window, the toolless agent, the
mislabelled control arm - returned a plausible result and a zero exit code.
The loud failures were all trivial to fix.
