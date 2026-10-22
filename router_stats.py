#!/usr/bin/env python3
"""router_stats.py - how much do Claude Code sessions delegate, and what does it save?

Measurement half of the "model router". Reads the local Claude Code transcripts

    ~/.claude/projects/<project>/<session>.jsonl                          (main thread)
    ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl     (subagents)
    ~/.claude/projects/<project>/<session>/subagents/agent-<id>.meta.json (agentType)

and reports, per session and in total: spend, subagent share, delegations by
agent type, main-thread lookups (Read/Grep/Glob) and the money saved by having
subagent work done on a cheaper model than the main one.

usage: router_stats.py [--since today|Nd|YYYY-MM-DD] [--json]

Notes on the data (verified against real transcripts):
  * One API message is written as several transcript lines (one per content
    block) sharing the same message.id. Usage is counted ONCE per message.id
    per file. output_tokens grows while a message streams, so the line with the
    largest output_tokens (the final one) is the one counted.
  * Tool-use blocks are de-duplicated by their tool_use id.
  * Cost is computed from the pricing table below; the table is the only thing
    that needs editing when prices or models change.

Stdlib only.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Pricing ($ per million tokens)
# --------------------------------------------------------------------------
# (input, output, cache_read). Cache writes: 5 min = 1.25 x input, 1 h = 2 x input.
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0

# model id (exact or prefix) -> (input, output, cache_read)
PRICING = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
}
# model key -> (prompt-token threshold, rate multiplier applied to all rates above it)
LONG_PROMPT = {
    "claude-haiku-5-5": (100_000, 5.0),
}

DELEGATION_TOOLS = ("Agent", "Task")
LOOKUP_TOOLS = ("Read", "Grep", "Glob")
NOTE = ("saved = subagent work repriced at the main model; it does not count the "
        "extra saving from keeping the main context smaller.")

_key_cache = {}


def pricing_key(model):
    """Return the PRICING key for a model string, or None if unknown.

    Exact id wins; otherwise the longest key that is a prefix of the id. A
    suffix that looks like a further version number ("-1", "-2-3") is not
    accepted, so a future "claude-fable-5-2" is reported as unknown instead of
    being silently priced as "claude-fable-5".
    """
    if model in _key_cache:
        return _key_cache[model]
    found = None
    if model in PRICING:
        found = model
    elif model:
        for key in sorted(PRICING, key=len, reverse=True):
            if model.startswith(key):
                rest = model[len(key):]
                if re.match(r"-\d{1,2}(-|$)", rest):
                    continue
                found = key
                break
    _key_cache[model] = found
    return found


def message_cost(model, msg):
    """Cost in dollars of one message's usage priced as `model`; None if unknown model."""
    key = pricing_key(model)
    if key is None:
        return None
    rin, rout, rcr = PRICING[key]
    threshold = LONG_PROMPT.get(key)
    if threshold:
        prompt = msg["input"] + msg["cache_read"] + msg["c5"] + msg["c1"]
        if prompt > threshold[0]:
            rin, rout, rcr = rin * threshold[1], rout * threshold[1], rcr * threshold[1]
    total = (msg["input"] * rin
             + msg["output"] * rout
             + msg["cache_read"] * rcr
             + msg["c5"] * rin * CACHE_WRITE_5M
             + msg["c1"] * rin * CACHE_WRITE_1H)
    return total / 1_000_000.0


# --------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------
def parse_ts(value):
    """ISO timestamp (UTC 'Z' or offset) -> aware datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def parse_since(text):
    """today | Nd | YYYY-MM-DD -> aware local datetime cutoff."""
    text = (text or "today").strip().lower()
    if text == "today":
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
    m = re.fullmatch(r"(\d+)d", text)
    if m:
        return datetime.now().astimezone() - timedelta(days=int(m.group(1)))
    try:
        return datetime.strptime(text, "%Y-%m-%d").astimezone()  # local midnight
    except ValueError:
        raise SystemExit("router stats: bad --since %r (use today, Nd, or YYYY-MM-DD)" % text)


# --------------------------------------------------------------------------
# Transcript reading
# --------------------------------------------------------------------------
def read_transcript(path):
    """Parse one transcript file.

    Returns (messages, tools):
      messages: {message_id: {"ts", "model", input/output/cache_read/c5/c1}}
      tools:    {tool_use_id: (name, subagent_type, message_id)}
    """
    messages = {}
    tools = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return messages, tools
    with fh:
        for lineno, line in enumerate(fh):
            if '"assistant"' not in line:  # cheap prefilter before JSON parsing
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            model = msg.get("model")
            if not isinstance(usage, dict) or not model or model == "<synthetic>":
                continue
            ts = parse_ts(obj.get("timestamp"))
            if ts is None:
                continue
            mid = msg.get("id") or ("noid", obj.get("uuid") or lineno)

            out_tokens = usage.get("output_tokens") or 0
            existing = messages.get(mid)
            if existing is None or out_tokens >= existing["output"]:
                cc = usage.get("cache_creation")
                total_cc = usage.get("cache_creation_input_tokens") or 0
                if isinstance(cc, dict):
                    c5 = cc.get("ephemeral_5m_input_tokens") or 0
                    c1 = cc.get("ephemeral_1h_input_tokens") or 0
                    c1 += max(0, total_cc - c5 - c1)  # unexplained remainder -> 1h
                else:
                    c5, c1 = 0, total_cc  # no breakdown: treat everything as 1h
                messages[mid] = {
                    "ts": existing["ts"] if existing else ts,
                    "model": model,
                    "input": usage.get("input_tokens") or 0,
                    "output": out_tokens,
                    "cache_read": usage.get("cache_read_input_tokens") or 0,
                    "c5": c5,
                    "c1": c1,
                }

            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tid = block.get("id")
                    if not tid or tid in tools:
                        continue
                    inp = block.get("input")
                    sub = None
                    if isinstance(inp, dict):
                        sub = inp.get("subagent_type")
                    tools[tid] = (block.get("name"), sub or "general-purpose", mid)
    return messages, tools


def read_meta(agent_path):
    meta = Path(str(agent_path)[:-len(".jsonl")] + ".meta.json")
    try:
        with open(meta, encoding="utf-8") as fh:
            data = json.load(fh)
        agent_type = data.get("agentType") if isinstance(data, dict) else None
        return agent_type or "unknown"
    except (OSError, ValueError):
        return "unknown"


def mtime_ok(path, cutoff_epoch):
    try:
        return os.path.getmtime(path) >= cutoff_epoch
    except OSError:
        return False


def project_label(dirname):
    """Strip the leading '-home-<user>-' from a project directory name."""
    user = Path.home().name
    prefix = "-home-%s-" % user
    if dirname.startswith(prefix):
        return dirname[len(prefix):]
    return re.sub(r"^-home-[^-]+-", "", dirname)


def discover_sessions(projects_dir, cutoff_epoch):
    """Yield (project_dir_name, session_id, main_path_or_None, [agent_paths]) for sessions
    that have at least one file modified since the cutoff."""
    try:
        project_dirs = sorted(p for p in projects_dir.iterdir() if p.is_dir())
    except OSError:
        return
    for pdir in project_dirs:
        mains = {}
        agents = defaultdict(list)
        try:
            entries = list(pdir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix == ".jsonl":
                mains[entry.stem] = entry
            elif entry.is_dir():
                sub = entry / "subagents"
                if sub.is_dir():
                    for a in sub.glob("agent-*.jsonl"):
                        agents[entry.name].append(a)
        for sid in sorted(set(mains) | set(agents)):
            main = mains.get(sid)
            alist = sorted(agents.get(sid, []))
            touched = [p for p in ([main] if main else []) + alist if mtime_ok(p, cutoff_epoch)]
            if touched:
                yield pdir.name, sid, main, alist


def most_common(counter):
    return counter.most_common(1)[0][0] if counter else None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def analyze_session(pname, sid, main_path, agent_paths, cutoff, unknown_models):
    """Return a session dict, or None if it has no counted messages after the cutoff."""
    main_all, main_tools = read_transcript(main_path) if main_path else ({}, {})
    main_msgs = {k: v for k, v in main_all.items() if v["ts"] >= cutoff}

    counted_models = Counter(m["model"] for m in main_msgs.values())
    main_model = most_common(counted_models) or most_common(Counter(m["model"] for m in main_all.values()))
    main_key = pricing_key(main_model) if main_model else None

    def note_unknown(model):
        unknown_models[model] += 1

    # ---- main thread
    main_cost = 0.0
    for m in main_msgs.values():
        c = message_cost(m["model"], m)
        if c is None:
            note_unknown(m["model"])
        else:
            main_cost += c

    delegations = Counter()
    lookups = 0
    for name, sub, mid in main_tools.values():
        if mid not in main_msgs:
            continue
        if name in DELEGATION_TOOLS:
            delegations[sub] += 1
        elif name in LOOKUP_TOOLS:
            lookups += 1

    # ---- subagents
    subagents = {}
    sub_cost_total = 0.0
    saved = 0.0
    sub_msg_count = 0
    for apath in agent_paths:
        if not mtime_ok(apath, cutoff.timestamp()):
            continue
        amsgs, _ = read_transcript(apath)
        counted = [m for m in amsgs.values() if m["ts"] >= cutoff]
        if not counted:
            continue
        atype = read_meta(apath)
        entry = subagents.setdefault(atype, {"runs": 0, "messages": 0, "cost": 0.0,
                                              "models": Counter()})
        entry["runs"] += 1
        for m in counted:
            entry["messages"] += 1
            entry["models"][m["model"]] += 1
            sub_msg_count += 1
            actual = message_cost(m["model"], m)
            if actual is None:
                note_unknown(m["model"])
                continue
            entry["cost"] += actual
            sub_cost_total += actual
            if main_key is not None:
                saved += message_cost(main_model, m) - actual

    if not main_msgs and not sub_msg_count:
        return None

    total = main_cost + sub_cost_total
    return {
        "project": project_label(pname),
        "project_dir": pname,
        "session": sid[:8],
        "session_id": sid,
        "main_model": main_model,
        "main_messages": len(main_msgs),
        "subagent_messages": sub_msg_count,
        "main_cost": main_cost,
        "subagent_cost": sub_cost_total,
        "total_cost": total,
        "delegations": dict(delegations),
        "delegation_count": sum(delegations.values()),
        "lookups": lookups,
        "subagents": {
            t: {"model": most_common(e["models"]), "models": dict(e["models"]),
                "runs": e["runs"], "messages": e["messages"], "cost": e["cost"]}
            for t, e in sorted(subagents.items())
        },
        "saved": saved,
        "saved_computable": main_key is not None or sub_msg_count == 0,
    }


def build_report(sessions, cutoff, unknown_models):
    sessions = sorted(sessions, key=lambda s: s["total_cost"], reverse=True)
    deleg = Counter()
    by_type = {}
    for s in sessions:
        deleg.update(s["delegations"])
        for t, e in s["subagents"].items():
            agg = by_type.setdefault(t, {"runs": 0, "messages": 0, "cost": 0.0, "models": Counter()})
            agg["runs"] += e["runs"]
            agg["messages"] += e["messages"]
            agg["cost"] += e["cost"]
            agg["models"].update(e["models"])
    total = sum(s["total_cost"] for s in sessions)
    main = sum(s["main_cost"] for s in sessions)
    sub = sum(s["subagent_cost"] for s in sessions)
    saved = sum(s["saved"] for s in sessions)
    would = total + saved
    return {
        "since": cutoff.isoformat(timespec="seconds"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sessions": sessions,
        "totals": {
            "sessions": len(sessions),
            "main_cost": main,
            "subagent_cost": sub,
            "total_cost": total,
            "subagent_share_pct": (100.0 * sub / total) if total else 0.0,
            "delegations": dict(sorted(deleg.items())),
            "delegation_count": sum(deleg.values()),
            "lookups": sum(s["lookups"] for s in sessions),
            "saved": saved,
            "would_have_been_cost": would,
            "saved_pct_of_would_have_been": (100.0 * saved / would) if would else 0.0,
            "subagents_by_type": {
                t: {"model": most_common(a["models"]), "models": dict(a["models"]),
                    "runs": a["runs"], "messages": a["messages"], "cost": a["cost"]}
                for t, a in sorted(by_type.items())
            },
        },
        "unknown_models": dict(unknown_models),
        "note": NOTE,
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def round_floats(obj, nd=6):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_floats(v, nd) for v in obj]
    return obj


def money(x):
    return "%.2f" % x if abs(x) >= 0.005 or x == 0 else "%.4f" % x


def short_project(name, width=30):
    return name if len(name) <= width else "~" + name[-(width - 1):]


def deleg_text(d):
    return " ".join("%s:%d" % (k, v) for k, v in sorted(d.items())) or "-"


def unknown_warning(unknown_models):
    if not unknown_models:
        return None
    return ("warning: unknown model(s), tokens counted but cost unknown: "
            + ", ".join("%s (%d msgs)" % (m, n) for m, n in sorted(unknown_models.items())))


def render_text(report):
    sessions = report["sessions"]
    t = report["totals"]
    out = []
    since = datetime.fromisoformat(report["since"]).strftime("%Y-%m-%d %H:%M")
    out.append("router stats  since %s (local)  -  %d session%s"
               % (since, t["sessions"], "" if t["sessions"] == 1 else "s"))
    out.append("")
    if not sessions:
        out.append("no sessions with usage in this period.")
        out.append("")
        out.append(NOTE)
        return "\n".join(out)

    headers = ["PROJECT", "SESSION", "MAIN MODEL", "MSGS", "MAIN $", "SUBAG $", "TOTAL $",
               "DELEGATIONS", "LOOKUPS", "SAVED $"]
    rows = []
    for s in sessions:
        saved = money(s["saved"]) if s["saved_computable"] else "n/a"
        rows.append([short_project(s["project"]), s["session"], s["main_model"] or "-",
                     str(s["main_messages"]), money(s["main_cost"]), money(s["subagent_cost"]),
                     money(s["total_cost"]), deleg_text(s["delegations"]), str(s["lookups"]), saved])
    left = {0, 1, 2, 7}  # left-aligned columns, the rest right-aligned
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]

    def fmt(cells):
        parts = [c.ljust(widths[i]) if i in left else c.rjust(widths[i]) for i, c in enumerate(cells)]
        return "  ".join(parts).rstrip()

    out.append(fmt(headers))
    out.append(fmt(["-" * w for w in widths]))
    out.extend(fmt(r) for r in rows)
    out.append("")

    out.append("Totals")
    out.append("  total spend            $%s  (main $%s, subagents $%s)"
               % (money(t["total_cost"]), money(t["main_cost"]), money(t["subagent_cost"])))
    out.append("  subagent share         %.1f%% of spend" % t["subagent_share_pct"])
    out.append("  delegations            %s  (total %d)" % (deleg_text(t["delegations"]), t["delegation_count"]))
    out.append("  main-thread lookups    %d  (Read/Grep/Glob)" % t["lookups"])
    out.append("  saved                  $%s  (%.1f%% of the $%s it would have been)"
               % (money(t["saved"]), t["saved_pct_of_would_have_been"], money(t["would_have_been_cost"])))
    if t["subagents_by_type"]:
        out.append("  subagent spend by type")
        nw = max(len(n) for n in t["subagents_by_type"])
        mw = max(len(a["model"] or "-") for a in t["subagents_by_type"].values())
        for name, a in t["subagents_by_type"].items():
            out.append("    %s  %s  %3d run%s  $%s"
                       % (name.ljust(nw), (a["model"] or "-").ljust(mw), a["runs"],
                          " " if a["runs"] == 1 else "s", money(a["cost"])))
    out.append("")
    warning = unknown_warning(report["unknown_models"])
    if warning:
        out.append(warning)
    unpriced = [s for s in sessions if not s["saved_computable"]]
    if unpriced:
        out.append("note: saved not computed (main model has no price) for: "
                   + ", ".join("%s/%s" % (s["project"], s["session"]) for s in unpriced))
    out.append(NOTE)
    return "\n".join(out)


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="router stats",
        description="Measure subagent delegation and savings from local Claude Code transcripts.")
    ap.add_argument("--since", default="today", metavar="D",
                    help="today (default, local midnight), Nd (e.g. 7d), or YYYY-MM-DD")
    ap.add_argument("--json", action="store_true", help="emit one JSON object instead of a table")
    args = ap.parse_args(argv)

    cutoff = parse_since(args.since)
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    projects_dir = config_dir / "projects"

    unknown_models = Counter()
    sessions = []
    for pname, sid, main_path, agent_paths in discover_sessions(projects_dir, cutoff.timestamp()):
        s = analyze_session(pname, sid, main_path, agent_paths, cutoff, unknown_models)
        if s is not None:
            sessions.append(s)

    report = build_report(sessions, cutoff, unknown_models)
    if args.json:
        print(json.dumps(round_floats(report), indent=2, ensure_ascii=False))
        warning = unknown_warning(unknown_models)
        if warning:
            print(warning, file=sys.stderr)
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
