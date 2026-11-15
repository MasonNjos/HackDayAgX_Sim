"""Plain-language explanations: plan-change summaries and per-job "why?".

Owner: Person B. Templated text only (no network), so the demo never depends on an API.

  explain_plan(old_queues, plan, state, trigger) -> str   # plan.explanation
  why_text(job_id, record, state) -> str                 # rendered by GET /why/<job>

`record` is the scoring record the Fleet Agent stores in plan.why[job_id]
(the sim keeps the latest one per job in sim.why).
"""
from __future__ import annotations

from .models import BROKEN, DONE, IN_PROGRESS, Event, Plan, Stop, WorldState

VERBS = {"spray": "spraying", "mow": "mowing"}
PRIORITY = {1: "low", 2: "medium", 3: "high"}


# --- small helpers -------------------------------------------------------------

def _field_name(state: WorldState, job_id: str) -> str:
    job = state.jobs.get(job_id)
    if job is None:
        return job_id
    f = state.fields.get(job.field)
    return f.name if f else job.field


def job_phrase(state: WorldState, job_id: str) -> str:
    """'spraying River Flats'"""
    job = state.jobs.get(job_id)
    if job is None:
        return job_id
    return f"{VERBS.get(job.type, job.type + ' work on')} {_field_name(state, job_id)}"


def _jobs_in(stops: list[Stop], state: WorldState) -> list[str]:
    """Job ids in a queue, skipping refuels and jobs that are done or unknown."""
    return [s.target for s in stops
            if s.kind == "job" and s.target in state.jobs and state.jobs[s.target].status != DONE]


def _owners(queues: dict[str, list[Stop]], state: WorldState) -> dict[str, str]:
    return {jid: tid for tid, q in queues.items() for jid in _jobs_in(q, state)}


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _work_word(state: WorldState, tid: str) -> str:
    tr = state.tractors.get(tid)
    caps = tr.capabilities if tr else []
    return VERBS.get(caps[0], "work") if len(caps) == 1 else "work"


# --- plan diff -------------------------------------------------------------------

def diff_plans(old_queues: dict[str, list[Stop]] | None, new_queues: dict[str, list[Stop]],
               state: WorldState) -> dict:
    """Compare two plans job by job (done jobs ignored).

    Returns {"moved": {job: (old, new)}, "newly_assigned": {job: new}, "dropped": {job: old},
             "unchanged": [tractor], "reordered": [tractor]}.
    """
    old_queues = old_queues or {}
    old, new = _owners(old_queues, state), _owners(new_queues, state)
    out = {"moved": {}, "newly_assigned": {}, "dropped": {}, "unchanged": [], "reordered": []}
    for jid, tid in new.items():
        if jid not in old:
            out["newly_assigned"][jid] = tid
        elif old[jid] != tid:
            out["moved"][jid] = (old[jid], tid)
    for jid, tid in old.items():
        if jid not in new:
            out["dropped"][jid] = tid
    for tid in sorted(new_queues):
        o, n = _jobs_in(old_queues.get(tid, []), state), _jobs_in(new_queues[tid], state)
        if not n:
            continue
        if o == n:
            out["unchanged"].append(tid)
        elif set(o) == set(n):
            out["reordered"].append(tid)
    return out


# --- plan-change explanation -------------------------------------------------------

def _breakdown_lead(tid: str, old_queues: dict[str, list[Stop]] | None,
                    state: WorldState, t: int, what: str = "broke down") -> tuple[str, str | None]:
    """'T2 broke down at t=40 while spraying River Flats (60% done).' plus the job it was on.

    The current state is checked first: the sim has just released the job, the tractor
    was the last one to work it, and it is standing at that field. The last plan is
    only used to tell where it was heading (old_queues has completed stops removed).
    """
    tr = state.tractors.get(tid)
    loc = tr.location if tr else None
    head: Stop | None = None
    for s in (old_queues or {}).get(tid, []):
        if s.kind == "refuel" or (s.target in state.jobs and state.jobs[s.target].status != DONE):
            head = s
            break

    working = [j for j in state.jobs.values()
               if j.status != DONE and j.owner is None and j.remaining < 1.0 and j.completed_by
               and j.completed_by[-1] == tid and j.field in state.fields and state.job_node(j.id) == loc]
    if working:
        # Two part-done jobs on one field: prefer the one the last plan had at the head.
        j = next((w for w in working if head is not None and head.target == w.id), working[0])
        pct = round((1.0 - j.remaining) * 100)
        return f"{tid} {what} at t={t} while {job_phrase(state, j.id)} ({pct}% done).", j.id

    if head is None:
        return f"{tid} {what} at t={t} while idle.", None
    if head.kind == "refuel":
        depot = state.depots.get(head.target)
        if depot and depot.node == loc:
            return f"{tid} {what} at t={t} while refuelling at {head.target}.", None
        return f"{tid} {what} at t={t} on its way to refuel at {head.target}.", None
    field = _field_name(state, head.target)
    if head.target in state.jobs and state.jobs[head.target].field in state.fields \
            and state.job_node(head.target) == loc:
        return f"{tid} {what} at t={t} just as it reached {field} ({head.target}).", head.target
    return f"{tid} {what} at t={t} on its way to {field} ({head.target}).", head.target


def _stopped_tractors(old_queues: dict[str, list[Stop]] | None, state: WorldState) -> list[str]:
    """Tractors that went out of service since the last plan without a breakdown event
    (the sim stalls a tractor that runs dry): broken now, but the last plan gave them work."""
    return [tid for tid, q in sorted((old_queues or {}).items())
            if q and tid in state.tractors and state.tractors[tid].status == BROKEN]


def _lead(trigger: Event | None, old_queues, state: WorldState) -> tuple[str, str | None, str | None]:
    """(lead sentence, broken tractor id, job it was on)."""
    t = state.t
    if trigger is None:
        if old_queues is None:
            return f"Initial plan at t={t}.", None, None
        stopped = _stopped_tractors(old_queues, state)
        if not stopped:
            return f"Replanned at t={t}.", None, None
        lines = [_breakdown_lead(tid, old_queues, state, t, "went out of service") for tid in stopped]
        lead = f"Replanned at t={t}. " + " ".join(line for line, _ in lines)
        return lead, stopped[0], lines[0][1]
    p, k = trigger.params, trigger.kind
    if k == "breakdown":
        lead, job = _breakdown_lead(p.get("tractor", "?"), old_queues, state, t)
        return lead, p.get("tractor"), job
    if k == "repair":
        return f"{p.get('tractor')} was repaired at t={t} and is back in service.", None, None
    if k in ("route_blocked", "route_unblocked"):
        e = state.edges.get(p.get("edge", ""))
        if e and e.label:
            name = f"the {e.label}" if e.label == e.id else f"the {e.label} ({e.id})"
        else:
            name = f"road {p.get('edge')}"
        if k == "route_blocked":
            return f"{name[0].upper() + name[1:]} was blocked at t={t}; routes now avoid it.", None, None
        return f"{name[0].upper() + name[1:]} reopened at t={t}.", None, None
    if k == "priority_change":
        pr = p.get("priority")
        return (f"{p.get('job')} priority changed to {pr} ({PRIORITY.get(pr, pr)}) at t={t}; "
                f"queued jobs re-scored, jobs in progress untouched."), None, None
    if k == "new_job":
        d = p.get("job") or {}
        jid = d.get("id", "?")
        if jid in state.jobs:
            j = state.jobs[jid]
            # The sim ignores a new_job whose id already exists; spot that by comparing fields.
            same = (j.type == d.get("type") and j.field == d.get("field") and j.deadline == d.get("deadline")
                    and j.submitted_at == d.get("submitted_at", t))
            if not same:
                return f"New job {jid} was ignored at t={t}: a job with that id already exists.", None, None
            return (f"New job {jid} ({job_phrase(state, jid)}, deadline t={j.deadline}) "
                    f"submitted at t={t}."), None, None
        return f"New job {jid} submitted at t={t}.", None, None
    return f"t={t}: {k} {p}.", None, None


def _assignment_detail(jid: str, tid: str, why: dict, state: WorldState) -> str:
    rec = why.get(jid) or {}
    bits = []
    tr = state.tractors.get(tid)
    if tr:
        bits.append(tr.implement)
    if rec.get("distance_km") is not None:
        bits.append(f"{rec['distance_km']:g} km drive")
    if rec.get("eta") is not None:
        bits.append(f"ETA t={rec['eta']}")
    if rec.get("refuel_first"):
        bits.append("refuels first")
    return f"{tid} ({', '.join(bits)})" if bits else tid


def _unassignable_lines(plan: Plan, state: WorldState) -> list[str]:
    out = []
    for jid, rec in (plan.why or {}).items():
        if rec.get("chosen") is not None or jid not in state.jobs:
            continue
        if rec.get("reason"):
            reasons = rec["reason"]
        else:
            reasons = "; ".join(rec.get("rejected", {}).values()) or "no tractors available"
        out.append(f"{jid} ({job_phrase(state, jid)}) cannot be assigned right now: {reasons}.")
    return out


def _deadline_lines(plan: Plan) -> list[str]:
    out = []
    for w in plan.warnings:
        if "may miss its deadline" in w and not w.startswith("SAFETY"):
            out.append(w.split(" (")[0] + ".")
    return out


def explain_plan(old_queues: dict[str, list[Stop]] | None, plan: Plan, state: WorldState,
                 trigger: Event | None) -> str:
    lead, broken, broken_job = _lead(trigger, old_queues, state)
    parts = [lead]
    why = plan.why or {}

    if old_queues is None:  # nothing to diff against: describe the plan
        for tid in sorted(plan.queues):
            js = _jobs_in(plan.queues[tid], state)
            if js:
                parts.append(f"{tid}: {' > '.join(js)}.")
    else:
        d = diff_plans(old_queues, plan.queues, state)
        moved = dict(d["moved"])  # the broken tractor's old jobs land here too
        if broken_job and broken_job in moved:
            parts.append(f"Its remaining work went to {_assignment_detail(broken_job, moved.pop(broken_job)[1], why, state)}.")
        for jid, (src, dst) in sorted(moved.items()):
            parts.append(f"{jid} ({_field_name(state, jid)}) moved from {src} to "
                         f"{_assignment_detail(jid, dst, why, state)}.")
        for jid, dst in sorted(d["newly_assigned"].items()):
            parts.append(f"{jid} ({job_phrase(state, jid)}) assigned to {_assignment_detail(jid, dst, why, state)}.")
        for tid in d["reordered"]:
            parts.append(f"{tid}'s order is now {' > '.join(_jobs_in(plan.queues[tid], state))}.")
        # Dropped jobs with a "no tractor" record are covered by the unassignable lines below.
        for jid, src in sorted(d["dropped"].items()):
            rec = why.get(jid) or {}
            if rec.get("chosen", "") is None:
                continue
            reason = f": {rec['reason']}" if rec.get("reason") else ""
            parts.append(f"{jid} ({job_phrase(state, jid)}) was taken off {src}'s queue{reason}.")
        changed = len(parts) > 1 or bool(d["dropped"])
        unchanged = d["unchanged"]
        if unchanged and changed:
            parts.append(_join([f"{t}'s {_work_word(state, t)} order" for t in unchanged])
                         + (" was" if len(unchanged) == 1 else " were") + " unchanged.")
        if not changed:
            parts.append("No change to assignments.")

    parts += _unassignable_lines(plan, state)
    parts += _deadline_lines(plan)
    return " ".join(parts)


# --- per-job "why?" ------------------------------------------------------------------

def why_text(job_id: str, record: dict | None, state: WorldState) -> str:
    """Render a Fleet Agent scoring record as a short paragraph for /why/<job>."""
    job = state.jobs.get(job_id)
    if job is None:
        return f"Unknown job {job_id}."
    head = (f"{job_id} ({job_phrase(state, job_id)}, priority {job.priority} "
            f"({PRIORITY.get(job.priority, job.priority)}), deadline t={job.deadline})")
    status = ""
    if job.status == DONE:
        status = f"\nDone at t={job.completed_at} by {', '.join(job.completed_by)}."
    elif job.status == IN_PROGRESS and job.owner:
        status = (f"\nIn progress on {job.owner}, {round((1 - job.remaining) * 100)}% done; "
                  f"a job in progress is never moved unless its tractor breaks down.")
    if not record:
        return f"{head}: not planned yet." + status

    when = f" at t={record['planned_at']}" if record.get("planned_at") is not None else ""
    lines: list[str] = []
    if record.get("chosen") is None:
        reason = record.get("reason")
        if reason:
            lines.append(f"{head} could not be assigned{when}: {reason}.")
        else:
            lines.append(f"{head} could not be assigned{when}; no tractor can take it right now.")
            for tid, r in sorted(record.get("rejected", {}).items()):
                lines.append(f"- {tid}: {r}.")
        lines.append("It stays in the pool and is retried at every replan (e.g. after a repair).")
        return "\n".join(lines) + status

    tid = record["chosen"]
    lines.append(f"{head} was given to {tid}{when}.")
    if record.get("decided_by") == "claude":
        if record.get("claude_reason"):
            lines.append(f"Claude: {record['claude_reason'].rstrip('.')}.")
        g = record.get("greedy_choice")
        if g and g != tid:
            lines.append(f"Claude moved it from the greedy pick ({g}) after checking the schedule.")
    eta, dl = record.get("eta"), record.get("deadline", job.deadline)
    detail = []
    if record.get("distance_km") is not None:
        detail.append(f"{record['distance_km']:g} km drive")
    if eta is not None:
        slack = record.get("slack", dl - eta)
        detail.append(f"ETA t={eta} ({slack} min before the deadline)" if slack >= 0
                      else f"ETA t={eta} ({-slack} min late; the best available)")
    if record.get("refuel_first"):
        detail.append("refuels first")
    if detail:
        lines.append(f"{tid}: " + ", ".join(detail) + ".")
    c = record.get("cost") or {}
    if c:
        terms = [f"{c.get('travel_ticks', 0)} min extra travel", f"{c.get('wait_ticks', 0)} min until it can start"]
        if c.get("late_ticks"):
            terms.append(f"{c['late_ticks']} min late")
        if c.get("refuel_ticks"):
            terms.append(f"{c['refuel_ticks']} min refuelling")
        lines.append(f"Cost {c.get('total')} (lowest): " + ", ".join(terms) + ".")
    opts = record.get("options") or {}
    alts = record.get("alternatives") or {}
    for other in sorted(alts):
        o = opts.get(other, {})
        extra = f", ETA t={o['eta']}" if "eta" in o else ""
        lines.append(f"- {other} could do it too but costs more ({alts[other]}{extra}).")
    for other, r in sorted((record.get("rejected") or {}).items()):
        lines.append(f"- {other} rejected: {r}.")
    return "\n".join(lines) + status
