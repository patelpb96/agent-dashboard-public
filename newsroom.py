"""Newsroom simulation: a dummy multi-agent workload for the plane view.

An Editor-in-Chief (opus) runs a paper. Senior journalists (sonnet) each run a
desk of reporters (haiku). One edition goes:

  1. assign    editor -> seniors -> reporters (each reporter gets a beat; some
               reporters on the same desk are paired on a joint story)
  2. report    reporters write notes/<story>.md; for a pair, the second reporter
               reads the partner's notes and writes the joint draft
  3. review    each senior reads the desk's drafts and writes desk/<desk>-review.md
               with APPROVE / REVISE per story
  4. revise    reporters fix REVISE stories once (the loop back down the chain)
  5. brief     each senior files desk/<desk>-brief.md to the editor
  6. publish   the editor reads the briefs, signs off and writes edition/front-page.md

Every agent is one Claude Code session (one dot on the plane). Its later turns
resume the same session. Names, roles, models and the reporting line go into a
roster file in ~/.claude/agent-roster/ (see paths.py), which the dashboard reads.

  python newsroom.py --mock --reporters 40      free: synthetic transcripts, no API calls
  python newsroom.py --reporters 2              live, smallest paper (4 agents)
  python newsroom.py --reporters 8 --seniors 2  live, bigger paper
  python newsroom.py --clean                    delete mock transcripts, workspaces and rosters

Live calls use a short custom system prompt, file tools only, no MCP, no
CLAUDE.md (--safe-mode), a per-call dollar cap and a timeout.
"""
import argparse
import json
import math
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from paths import PROJECTS_DIR, ROSTER_DIR, SIM_ROOT
MOCK_PREFIX = "newsroom-mock-"

MODEL = {0: "opus", 1: "sonnet", 2: "haiku"}
BUDGET_USD = {"opus": 0.40, "sonnet": 0.12, "haiku": 0.03}   # per call, enforced by the CLI
CALL_TIMEOUT_S = 150
TOOLS = "Read,Write,Edit,Glob"

# Two-word names (adjective + noun) instead of human names.
# Each tier has its own vocabulary, so the editor sounds grander than a reporter and no
# two tiers can produce the same name.
NAME_WORDS = {
    0: (["Sovereign", "Exalted", "Resplendent", "Unwavering", "Magnanimous", "Venerable", "Imperious",
         "Luminous"],
        ["Verdict", "Mandate", "Arbiter", "Imprimatur", "Codex", "Edict", "Providence", "Meridian"]),
    1: (["Abiding", "Solemn", "Judicious", "Scrupulous", "Discerning", "Austere", "Measured", "Lambent",
         "Patient", "Exacting"],
        ["Concordance", "Rubric", "Precedent", "Canon", "Assay", "Scrutiny", "Testament", "Reckoning",
         "Ledger", "Lantern"]),
    2: (["Lucid", "Wistful", "Diligent", "Errant", "Candid", "Furtive", "Steadfast", "Restless", "Earnest",
         "Sardonic", "Vigilant", "Wandering", "Fervent", "Mercurial", "Placid", "Tenacious", "Curious",
         "Laconic", "Gallant", "Pensive", "Wry", "Ardent", "Brisk", "Meticulous", "Rueful", "Sanguine",
         "Stoic", "Zealous", "Plaintive", "Nimble", "Dauntless", "Oblique", "Jubilant", "Somber", "Keen",
         "Quiet"],
        ["Echo", "Quill", "Cipher", "Margin", "Footnote", "Dispatch", "Vigil", "Axiom", "Gazette", "Rumor",
         "Byline", "Corollary", "Herald", "Prism", "Index", "Inkwell", "Signal", "Almanac", "Chronicle",
         "Parallax", "Verity", "Errata", "Glance", "Lens", "Murmur", "Census", "Compass", "Refrain", "Coda",
         "Sextant", "Query", "Inference", "Precept", "Augury", "Tally", "Draft"]),
}


def monitor_names(tier, count, rng):
    """`count` distinct names for one tier; past the vocabulary they get a numeral."""
    adjs, nouns = NAME_WORDS[tier]
    pool = [f"{a} {n}" for a in adjs for n in nouns]
    names = rng.sample(pool, min(count, len(pool)))
    return names + [f"{pool[i % len(pool)]} {i // len(pool) + 1}" for i in range(len(names), count)]
BEATS = ["city hall", "schools", "transit", "housing", "local business", "public health",
         "courts", "weather", "arts", "sports", "environment", "science", "tech", "food",
         "police", "elections", "parks", "libraries", "labor", "real estate"]
DESKS = ["Metro", "Features", "Business", "Sports", "Science", "Culture", "Politics", "Lifestyle"]

PERSONA = ("You are {name}, {role} at Agent Report, a small fictional newspaper in the "
           "fictional town of Maple Falls. Everything you report is invented for a test run, "
           "so make it plausible but short. Work only with the files named in the task, in the "
           "current folder. Be terse: no preamble, no summaries in chat. When done, reply DONE.")


def slug(text):
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---- staffing and assignments (pure, tested) ------------------------------

def staff(reporters, seniors=None, rng=None):
    """Editor, seniors and reporters, with names, models, tiers and desks."""
    rng = rng or random.Random(0)
    if seniors is None:
        seniors = 1 if reporters <= 4 else 2
    seniors = max(1, min(seniors, len(DESKS), max(1, reporters)))
    # Names are drawn per run, so several editions on the plane don't share a cast.
    people = [{"name": monitor_names(0, 1, rng)[0], "role": "Editor-in-Chief", "tier": 0, "desk": None}]
    desks = DESKS[:seniors]
    for name, desk in zip(monitor_names(1, seniors, rng), desks):
        people.append({"name": name, "role": f"Senior journalist, {desk} desk", "tier": 1, "desk": desk})
    for i, name in enumerate(monitor_names(2, reporters, rng)):
        desk = desks[i % seniors]
        people.append({"name": name, "role": f"Reporter, {desk} desk", "tier": 2, "desk": desk})
    for p in people:
        p["model"] = MODEL[p["tier"]]
        p["sessionId"] = str(uuid.uuid4())
    editor = people[0]
    by_desk = {p["desk"]: p for p in people if p["tier"] == 1}
    for p in people:
        p["reportsTo"] = editor["sessionId"] if p["tier"] == 1 else (
            by_desk[p["desk"]]["sessionId"] if p["tier"] == 2 else None)
    return people


def assign(people, pair_rate=0.35, rng=None):
    """Stories: one per reporter, except pairs on the same desk who share one."""
    rng = rng or random.Random(1)
    stories = []
    beats = list(BEATS)
    rng.shuffle(beats)
    for desk in [p["desk"] for p in people if p["tier"] == 1]:
        team = [p for p in people if p["tier"] == 2 and p["desk"] == desk]
        i = 0
        while i < len(team):
            beat = beats[len(stories) % len(beats)]
            name = f"{beat}-{len(stories) + 1}"
            if i + 1 < len(team) and rng.random() < pair_rate:
                stories.append({"slug": slug(name), "beat": beat, "desk": desk, "reporters": team[i:i + 2]})
                i += 2
            else:
                stories.append({"slug": slug(name), "beat": beat, "desk": desk, "reporters": [team[i]]})
                i += 1
    return stories


def parse_review(text, slugs):
    """{slug: note or None} from a review file; a story not mentioned is approved."""
    out = {s: None for s in slugs}
    for line in text.splitlines():
        low = line.lower()
        for s in slugs:
            if s in low and "revise" in low:
                out[s] = line.split("REVISE", 1)[-1].strip(" :-") or "tighten it"
    return out


# ---- roster (what the dashboard reads) -------------------------------------

class Roster:
    def __init__(self, run, people):
        self.path = ROSTER_DIR / f"newsroom-{run}.json"
        self.lock = threading.Lock()
        self.data = {"run": run, "title": "Agent Report", "agents": [
            {k: p[k] for k in ("sessionId", "name", "role", "model", "tier", "reportsTo")}
            | {"team": p["desk"], "state": "idle"} for p in people], "events": []}
        self._save()

    def _save(self):
        ROSTER_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def event(self, src, dst, kind):
        with self.lock:
            self.data["events"].append({"from": src["sessionId"], "to": dst["sessionId"],
                                        "kind": kind, "at": time.time()})
            self._save()

    def state(self, person, state):
        with self.lock:
            for a in self.data["agents"]:
                if a["sessionId"] == person["sessionId"]:
                    a["state"] = state
            self._save()


# ---- actors: live Claude sessions, or synthetic transcripts ---------------

class LiveActor:
    def __init__(self, workdir):
        self.workdir = workdir
        self.exe = shutil.which("claude")
        if not self.exe:
            sys.exit("claude CLI not found on PATH")
        self.started = set()
        self.cost = {}
        self.lock = threading.Lock()

    def run(self, person, prompt, steps, rng):
        model = person["model"]
        session = ["--resume", person["sessionId"]] if person["sessionId"] in self.started \
            else ["--session-id", person["sessionId"]]
        self.started.add(person["sessionId"])
        cmd = [self.exe, "-p", prompt, "--model", model, *session,
               "--system-prompt", PERSONA.format(**person), "--tools", TOOLS,
               "--strict-mcp-config", "--safe-mode", "--permission-mode", "acceptEdits",
               "--max-budget-usd", str(BUDGET_USD[model]), "--output-format", "json"]
        try:
            out = subprocess.run(cmd, cwd=self.workdir, capture_output=True, timeout=CALL_TIMEOUT_S)
            data = json.loads(out.stdout.decode("utf-8", errors="replace") or "{}")
        except subprocess.TimeoutExpired:
            print(f"  ! {person['name']}: timed out")
            return
        except ValueError:
            print(f"  ! {person['name']}: no JSON result ({out.stderr[:200]!r})")
            return
        usd = data.get("total_cost_usd") or 0.0
        with self.lock:
            self.cost[model] = self.cost.get(model, 0.0) + usd
        if data.get("is_error"):
            print(f"  ! {person['name']}: {data.get('subtype') or data.get('result')}")

    def read(self, rel):
        try:
            return (self.workdir / rel).read_text(encoding="utf-8")
        except OSError:
            return ""


class MockActor:
    """Writes transcript lines that look like real tool calls, at a human-watchable pace."""

    def __init__(self, workdir, run, speed):
        self.workdir = workdir
        self.project = PROJECTS_DIR / f"{MOCK_PREFIX}{run}"
        self.project.mkdir(parents=True, exist_ok=True)
        self.speed = speed
        self.cost = {}
        self.verdicts = {}

    def _write(self, person, entry):
        entry |= {"timestamp": now_iso(), "sessionId": person["sessionId"], "uuid": str(uuid.uuid4())}
        with (self.project / f"{person['sessionId']}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def run(self, person, prompt, steps, rng):
        self._write(person, {"type": "user", "message": {"role": "user", "content": prompt}})
        for tool, inp in steps:
            time.sleep(rng.uniform(0.6, 2.2) / self.speed)  # "thinking"
            tid = "toolu_" + uuid.uuid4().hex[:20]
            self._write(person, {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": tool, "input": inp}]}})
            time.sleep(rng.uniform(0.4, 2.5 if tool in ("WebSearch", "Write") else 1.0) / self.speed)
            self._write(person, {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})

    def read(self, rel):
        return self.verdicts.get(rel, "")


# ---- the edition -----------------------------------------------------------

class Newsroom:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random(args.seed)
        self.run_id = datetime.now().strftime("%m%d-%H%M%S")
        self.people = staff(args.reporters, args.seniors, self.rng)
        self.editor = self.people[0]
        self.seniors = [p for p in self.people if p["tier"] == 1]
        self.stories = assign(self.people, args.pair_rate, self.rng)
        self.workdir = SIM_ROOT / (("mock-" if args.mock else "") + self.run_id)
        for sub in ("notes", "drafts", "desk", "edition"):
            (self.workdir / sub).mkdir(parents=True, exist_ok=True)
        self.actor = MockActor(self.workdir, self.run_id, args.speed) if args.mock else LiveActor(self.workdir)
        self.roster = Roster(("mock-" if args.mock else "") + self.run_id, self.people)
        self.pool = ThreadPoolExecutor(max_workers=args.parallel)

    def p(self, rel):
        return str(self.workdir / rel)

    def act(self, person, prompt, steps):
        self.roster.state(person, "working")
        try:
            self.actor.run(person, prompt, steps, random.Random(self.rng.random()))
        finally:
            self.roster.state(person, "idle")

    def parallel(self, jobs):
        for f in [self.pool.submit(self.act, *job) for job in jobs]:
            f.result()

    def desk_of(self, senior):
        return [s for s in self.stories if s["desk"] == senior["desk"]]

    def senior_for(self, story):
        return next(s for s in self.seniors if s["desk"] == story["desk"])

    def phase(self, name):
        print(f"[{time.strftime('%H:%M:%S')}] {name}")

    def go(self):
        a = self.args
        print(f"Agent Report, run {self.run_id}: {len(self.people)} agents "
              f"(1 opus, {len(self.seniors)} sonnet, {a.reporters} haiku), {len(self.stories)} stories, "
              f"{'MOCK' if a.mock else 'LIVE'}\n  workspace {self.workdir}\n  roster    {self.roster.path}")
        for s in self.people:
            print(f"  {s['name']:<20} {s['role']:<32} {s['model']}")

        self.phase("assign")
        for senior in self.seniors:
            self.roster.event(self.editor, senior, "assigns")
        for story in self.stories:
            for r in story["reporters"]:
                self.roster.event(self.senior_for(story), r, "assigns")

        self.phase("report")
        jobs = []
        for st in self.stories:
            first = st["reporters"][0]
            note, draft = f"notes/{st['slug']}.md", f"drafts/{st['slug']}.md"
            web = [("WebSearch", {"query": f"Maple Falls {st['beat']}"})] if a.mock and self.rng.random() < 0.3 else []
            if len(st["reporters"]) == 1:
                jobs.append((first, f"Beat: {st['beat']}. Write 3 bullet facts to {note}, then a 120-word "
                                    f"news story with a headline to {draft}.",
                             web + [("Write", {"file_path": self.p(note)}), ("Write", {"file_path": self.p(draft)})]))
            else:
                jobs.append((first, f"Beat: {st['beat']}. You are reporting with a partner. Write 4 bullet "
                                    f"facts and one quote to {note}.",
                             web + [("Write", {"file_path": self.p(note)})]))
        self.parallel(jobs)
        jobs = []
        for st in self.stories:
            if len(st["reporters"]) == 2:
                first, second = st["reporters"]
                self.roster.event(first, second, "shares notes")
                note, draft = f"notes/{st['slug']}.md", f"drafts/{st['slug']}.md"
                jobs.append((second, f"Your partner {first['name']} filed notes in {note}. Read them and write a "
                                     f"150-word joint story with a headline and both bylines to {draft}.",
                             [("Read", {"file_path": self.p(note)}), ("Write", {"file_path": self.p(draft)})]))
        self.parallel(jobs)
        for st in self.stories:
            self.roster.event(st["reporters"][-1], self.senior_for(st), "files")

        self.phase("desk review")
        jobs = []
        for senior in self.seniors:
            mine = self.desk_of(senior)
            review = f"desk/{slug(senior['desk'])}-review.md"
            drafts = ", ".join(f"drafts/{s['slug']}.md" for s in mine)
            if a.mock:
                self.actor.verdicts[review] = "\n".join(
                    f"{s['slug']}: " + ("REVISE add a second source" if self.rng.random() < 0.3 else "APPROVE")
                    for s in mine)
            jobs.append((senior, f"Review your desk's drafts: {drafts}. Write {review} with exactly one line "
                                 f"per story: '<file stem>: APPROVE' or '<file stem>: REVISE <one short note>'. "
                                 f"Ask for at most one revision.",
                         [("Read", {"file_path": self.p(f"drafts/{s['slug']}.md")}) for s in mine]
                         + [("Write", {"file_path": self.p(review)})]))
        self.parallel(jobs)

        self.phase("revise")
        jobs, revised = [], []
        for senior in self.seniors:
            mine = self.desk_of(senior)
            verdicts = parse_review(self.actor.read(f"desk/{slug(senior['desk'])}-review.md"), [s["slug"] for s in mine])
            for st in mine:
                if verdicts[st["slug"]]:
                    revised.append(st)
                    r = st["reporters"][-1]
                    self.roster.event(senior, r, "revise")
                    draft = f"drafts/{st['slug']}.md"
                    jobs.append((r, f"Your editor {senior['name']} says: {verdicts[st['slug']]}. Revise {draft} "
                                    f"in place, keep it under 170 words.",
                                 [("Read", {"file_path": self.p(draft)}), ("Edit", {"file_path": self.p(draft)})]))
        print(f"  {len(jobs)} revision(s)")
        self.parallel(jobs)
        for st in revised:
            self.roster.event(st["reporters"][-1], self.senior_for(st), "files")

        self.phase("brief")
        jobs = []
        for senior in self.seniors:
            mine = self.desk_of(senior)
            brief = f"desk/{slug(senior['desk'])}-brief.md"
            jobs.append((senior, f"Write {brief} for the Editor-in-Chief: for each of "
                                 f"{', '.join(s['slug'] for s in mine)} give the headline from drafts/<stem>.md and "
                                 f"one line on why it matters. Under 120 words.",
                         [("Read", {"file_path": self.p(f"drafts/{s['slug']}.md")}) for s in mine[:3]]
                         + [("Write", {"file_path": self.p(brief)})]))
        self.parallel(jobs)
        for senior in self.seniors:
            self.roster.event(senior, self.editor, "briefs")

        self.phase("publish")
        briefs = [f"desk/{slug(s['desk'])}-brief.md" for s in self.seniors]
        self.act(self.editor, f"Read the desk briefs ({', '.join(briefs)}). Write edition/front-page.md: the "
                              f"paper's name and today's date, a lead story and up to 5 more headlines, each with "
                              f"its desk, then one line 'Signed off: {self.editor['name']}, Editor-in-Chief'. "
                              f"Under 200 words. Do not read the drafts.",
                 [("Read", {"file_path": self.p(b)}) for b in briefs]
                 + [("Write", {"file_path": self.p("edition/front-page.md")})])
        for senior in self.seniors:
            self.roster.event(self.editor, senior, "signed off")
        for p in self.people:
            self.roster.state(p, "done")
        self.pool.shutdown()

        total = sum(self.actor.cost.values())
        print(f"[{time.strftime('%H:%M:%S')}] published: {self.workdir / 'edition' / 'front-page.md'}")
        if not a.mock:
            print("  cost: " + ", ".join(f"{m} ${c:.4f}" for m, c in sorted(self.actor.cost.items()))
                  + f"  total ${total:.4f}")


def clean():
    removed = 0
    for d in PROJECTS_DIR.glob(MOCK_PREFIX + "*"):
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    for f in ROSTER_DIR.glob("newsroom-mock-*.json"):
        f.unlink(missing_ok=True)
        removed += 1
    for d in SIM_ROOT.glob("mock-*"):
        shutil.rmtree(d, ignore_errors=True)
    print(f"removed {removed} mock transcript folders / rosters")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reporters", type=int, default=2, help="haiku reporters (default 2)")
    ap.add_argument("--seniors", type=int, default=None, help="sonnet seniors (default 1, or 2 above 4 reporters)")
    ap.add_argument("--pair-rate", type=float, default=0.35, help="chance two desk-mates share a story")
    ap.add_argument("--parallel", type=int, default=4, help="agents working at once (default 4)")
    ap.add_argument("--mock", action="store_true", help="synthetic transcripts, no API calls")
    ap.add_argument("--speed", type=float, default=1.0, help="mock pace multiplier")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--clean", action="store_true", help="remove mock transcripts, workspaces and rosters (live runs are kept)")
    args = ap.parse_args(argv)
    if args.clean:
        return clean()
    if not args.mock and args.reporters > 12:
        sys.exit("live runs are capped at 12 reporters; use --mock for bigger papers")
    Newsroom(args).go()


if __name__ == "__main__":
    main()
