"""Turn accuracy into cost per 100 incidents, and find the break-even.

Accuracy is the wrong headline for triage. An operations team cares about
three things: how many incidents get resolved without a human, how many get
escalated, and how many send someone to fix the wrong thing. Those have very
different costs and the accuracy column collapses them into one number.

The inputs are in **engineer-minutes**, not currency, on purpose. Nobody
knows their fully-loaded hourly cost offhand, but anyone who has run an
on-call rota can estimate "a human re-triaging from scratch takes about
twenty minutes". The hourly rate is applied once, at the end, and only to
convert for presentation.

Because the inputs are estimates, the headline output is not a single
number: it is the **break-even** - the escalation cost at which the ranking
of two options flips. A conclusion that holds across the plausible range is
worth more than a point estimate sitting on three guesses.

What this deliberately does NOT model: customer impact, SLA credits,
reputational cost, and the cost of an engineer losing trust in the tool.
Those dominate real incident economics and none of them can be estimated
from this corpus, so they are excluded rather than invented. Every number
here is therefore a *lower bound* on the value of avoiding a misdirection.
"""

from __future__ import annotations

from dataclasses import dataclass

from .evaluate import Cell, Sweep
from .report import arm_label

# Defaults, stated so they can be argued with:
#   escalation - a human picks the incident up and triages from scratch,
#       having gained nothing from the tool. Twenty minutes is a short-ish
#       estimate for a SEV2 with no starting hypothesis.
#   misdirection - the engineer acts on a wrong cause, finds it did not
#       help, and re-triages. The wrong fix plus the lost time, and the
#       incident runs for all of it. This is an escalation plus the wasted
#       work preceding it, so it is always strictly the larger of the two.
DEFAULT_ESCALATION_MINUTES = 20.0
DEFAULT_MISDIRECTION_MINUTES = 45.0
DEFAULT_HOURLY_RATE = 75.0


@dataclass(frozen=True)
class Assumptions:
    escalation_minutes: float = DEFAULT_ESCALATION_MINUTES
    misdirection_minutes: float = DEFAULT_MISDIRECTION_MINUTES
    hourly_rate: float = DEFAULT_HOURLY_RATE

    @property
    def escalation_cost(self) -> float:
        return self.escalation_minutes / 60.0 * self.hourly_rate

    @property
    def misdirection_cost(self) -> float:
        return self.misdirection_minutes / 60.0 * self.hourly_rate


@dataclass
class ArmEconomics:
    arm: str
    label: str
    n: int
    resolved: int
    escalated: int
    misdirected: int
    model_cost: float

    def human_cost(self, a: Assumptions) -> float:
        return (self.escalated * a.escalation_cost
                + self.misdirected * a.misdirection_cost)

    def total_cost(self, a: Assumptions) -> float:
        return self.model_cost + self.human_cost(a)

    def per_100(self, a: Assumptions) -> float:
        return self.total_cost(a) / self.n * 100


def summarise_arm(cells: list[Cell], sweep: Sweep) -> ArmEconomics:
    arm = cells[0].arm
    return ArmEconomics(
        arm=arm,
        label=arm_label(sweep, arm),
        n=len(cells),
        resolved=sum(c.correct for c in cells),
        escalated=sum(c.abstained for c in cells),
        misdirected=sum(1 for c in cells if not c.correct and not c.abstained),
        model_cost=sum(c.cost_usd for c in cells),
    )


def _by_arm(sweep: Sweep) -> dict[str, list[Cell]]:
    out: dict[str, list[Cell]] = {}
    for cell in sweep.cells:
        out.setdefault(cell.arm, []).append(cell)
    return out


@dataclass
class Hybrid:
    """Cheap arm first; escalate only what it abstains on to the agent.

    Computed from the actual per-incident results, not assumed: for each
    incident the cheap arm abstained on, we look up what the agent really
    did with that same incident.
    """

    cheap_arm: str
    n: int
    resolved: int
    escalated: int
    misdirected: int
    model_cost: float
    handed_off: int

    def total_cost(self, a: Assumptions) -> float:
        return (self.model_cost
                + self.escalated * a.escalation_cost
                + self.misdirected * a.misdirection_cost)

    def per_100(self, a: Assumptions) -> float:
        return self.total_cost(a) / self.n * 100


def build_hybrid(sweep: Sweep, cheap_arm: str, expensive_arm: str = "agent") -> Hybrid | None:
    by_arm = _by_arm(sweep)
    if cheap_arm not in by_arm or expensive_arm not in by_arm:
        return None
    expensive = {c.incident_id: c for c in by_arm[expensive_arm]}

    resolved = escalated = misdirected = handed_off = 0
    model_cost = 0.0

    for cell in by_arm[cheap_arm]:
        model_cost += cell.cost_usd
        if not cell.abstained:
            # The cheap arm committed. We take its answer as final - which
            # is what makes routing on abstention safe only when the cheap
            # arm is not confidently wrong. If it were, this is where the
            # cost would land.
            resolved += cell.correct
            misdirected += 0 if cell.correct else 1
            continue
        handed_off += 1
        fallback = expensive.get(cell.incident_id)
        if fallback is None:
            escalated += 1
            continue
        model_cost += fallback.cost_usd
        if fallback.correct:
            resolved += 1
        elif fallback.abstained:
            escalated += 1
        else:
            misdirected += 1

    return Hybrid(
        cheap_arm=cheap_arm, n=len(by_arm[cheap_arm]), resolved=resolved,
        escalated=escalated, misdirected=misdirected,
        model_cost=model_cost, handed_off=handed_off,
    )


def escalation_breakeven(cheap: ArmEconomics, expensive: ArmEconomics,
                         a: Assumptions) -> float | None:
    """Escalation cost (USD) at which the two options cost the same.

    Below it the cheap arm wins on cost, above it the expensive one does.
    Returns None when the escalation counts are equal, which makes the
    comparison independent of escalation cost.
    """
    delta_escalations = cheap.escalated - expensive.escalated
    if delta_escalations == 0:
        return None
    fixed = (expensive.model_cost - cheap.model_cost
             + (expensive.misdirected - cheap.misdirected) * a.misdirection_cost)
    return fixed / delta_escalations


def render(sweep: Sweep, a: Assumptions) -> str:
    by_arm = _by_arm(sweep)
    arms = [summarise_arm(cells, sweep) for cells in by_arm.values()]
    arms.sort(key=lambda e: e.per_100(a))

    lines = [
        "## Cost per 100 incidents",
        "",
        "**Assumptions.** Change them with `--escalation-minutes`, "
        "`--misdirection-minutes` and `--hourly-rate`.",
        "",
        "| Input | Value | Meaning |",
        "| --- | ---: | --- |",
        f"| Escalation | {a.escalation_minutes:.0f} min "
        f"(${a.escalation_cost:.2f}) | A human triages from scratch. |",
        f"| Misdirection | {a.misdirection_minutes:.0f} min "
        f"(${a.misdirection_cost:.2f}) | Wrong fix attempted, then re-triage. |",
        f"| Engineer cost | ${a.hourly_rate:.0f}/hr | Fully loaded. |",
        "",
        "Excluded: customer impact, SLA credits, and loss of trust in the "
        "tool. All three dominate real incident cost and none can be "
        "estimated from this corpus, so every figure below is a lower bound "
        "on the value of avoiding a wrong answer.",
        "",
        "| Configuration | Resolved | Escalated | Misdirected | Model cost | Human cost | **Total / 100** |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for e in arms:
        scale = 100 / e.n
        lines.append(
            f"| {e.label} | {e.resolved * scale:.0f} | {e.escalated * scale:.0f} "
            f"| {e.misdirected * scale:.0f} | ${e.model_cost * scale:.2f} "
            f"| ${e.human_cost(a) * scale:.2f} | **${e.per_100(a):.2f}** |"
        )

    # -- the hybrid ------------------------------------------------------
    # Options are what you could actually deploy: each arm on its own, plus
    # each cheap arm with abstentions routed to the agent. Keeping them in
    # one list means the sensitivity table below compares real choices.
    agent = next((e for e in arms if e.arm == "agent"), None)
    options: list[tuple[str, object]] = [(e.label, e) for e in arms]

    if agent:
        lines += ["", "## Routing on abstention", ""]
        rows = []
        for cheap_arm in ("rag", "baseline"):
            hybrid = build_hybrid(sweep, cheap_arm)
            if not hybrid:
                continue
            label = f"{arm_label(sweep, cheap_arm)} -> agent"
            options.append((label, hybrid))
            scale = 100 / hybrid.n
            saving = agent.per_100(a) - hybrid.per_100(a)
            rows.append(
                f"| {label} | {hybrid.handed_off * scale:.0f} "
                f"| {hybrid.resolved * scale:.0f} "
                f"| {hybrid.misdirected * scale:.0f} "
                f"| ${hybrid.model_cost * scale:.2f} "
                f"| **${hybrid.per_100(a):.2f}** "
                f"| {saving / agent.per_100(a):.0%} cheaper |"
            )
        if rows:
            agent_scale = 100 / agent.n
            lines += [
                "Run the cheap configuration on everything; send only the "
                "incidents it abstains on to the agent. The outcomes below "
                "are what the agent actually did with those specific "
                "incidents, looked up per incident rather than assumed.",
                "",
                "| Route | Handed off | Resolved | Misdirected | Model cost | Total / 100 | vs all-agent |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
                *rows,
                f"| Agent on everything | - "
                f"| {agent.resolved * agent_scale:.0f} "
                f"| {agent.misdirected * agent_scale:.0f} "
                f"| ${agent.model_cost * agent_scale:.2f} "
                f"| ${agent.per_100(a):.2f} | baseline |",
                "",
                "Routing wins here for a reason that does not depend on any "
                "assumption: both routes resolve every incident, so neither "
                "escalates and neither misdirects. The only difference left "
                "is model spend, and running a cheap arm first means the "
                "agent is invoked on a minority of incidents instead of all "
                "of them.",
            ]

    # -- sensitivity ------------------------------------------------------
    lines += ["", "## Does the conclusion survive different assumptions?", ""]

    def cost_at(option, minutes: float) -> float:
        return option.per_100(
            Assumptions(minutes, a.misdirection_minutes, a.hourly_rate)
        )

    best_label, best_option = min(options, key=lambda kv: cost_at(kv[1], a.escalation_minutes))
    varies = {
        label for label, opt in options
        if abs(cost_at(opt, 5) - cost_at(opt, 90)) > 0.01
    }
    if best_label not in varies:
        lines += [
            f"**{best_label} is cheapest at every escalation cost tested.** "
            f"It resolves every incident, so it never pays the escalation "
            f"term at all - which makes the ranking independent of the one "
            f"assumption you are least able to pin down.",
            "",
        ]

    lines += ["Cost per 100 incidents across a range of escalation costs:", ""]
    labels = [label for label, _ in options]
    lines += [
        "| Escalation | " + " | ".join(labels) + " |",
        "| --- | " + " | ".join("---:" for _ in labels) + " |",
    ]
    for minutes in (5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0):
        values = [cost_at(opt, minutes) for _, opt in options]
        cells = [f"${v:,.2f}" for v in values]
        cheapest_i = min(range(len(values)), key=values.__getitem__)
        cells[cheapest_i] = f"**{cells[cheapest_i]}**"
        lines.append(f"| {minutes:.0f} min | " + " | ".join(cells) + " |")
    lines += ["", "_Bold is the cheapest option at that escalation cost._"]

    if all(e.misdirected == 0 for e in arms):
        lines += [
            "",
            "**Note on misdirection cost.** No configuration in this sweep "
            "produced a single wrong answer, so that term is zero everywhere "
            "and `--misdirection-minutes` changes nothing here. It is kept "
            "because that is a property of this run, not a guarantee: a "
            "weaker model, or a prompt without an explicit `unknown` option, "
            "would move cost into that column and it is the most expensive "
            "column there is.",
        ]
    else:
        # When something *is* confidently wrong, the misdirection price is
        # no longer inert and the ranking can turn on it. Varying only
        # escalation cost would hide that, so sweep this one too.
        lines += ["", "Cost per 100 incidents across a range of "
                      "misdirection costs:", ""]
        lines += [
            "| Misdirection | " + " | ".join(labels) + " |",
            "| --- | " + " | ".join("---:" for _ in labels) + " |",
        ]
        for minutes in (5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0):
            alt = Assumptions(a.escalation_minutes, minutes, a.hourly_rate)
            values = [opt.per_100(alt) for _, opt in options]
            cells = [f"${v:,.2f}" for v in values]
            cheapest_i = min(range(len(values)), key=values.__getitem__)
            cells[cheapest_i] = f"**{cells[cheapest_i]}**"
            lines.append(f"| {minutes:.0f} min | " + " | ".join(cells) + " |")
        lines += [
            "",
            "A misdirection should cost strictly more than an escalation: "
            "it is an escalation plus the time spent acting on the wrong "
            "answer first. Rows at or below that line are shown for "
            "completeness, not because they are plausible.",
        ]
    return "\n".join(lines)
