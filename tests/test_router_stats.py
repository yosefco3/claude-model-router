"""Tests for router_stats.py (stdlib unittest, synthetic transcripts in a temp CLAUDE_CONFIG_DIR)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "router_stats.py"
sys.path.insert(0, str(ROOT))
import router_stats  # noqa: E402

OPUS = "claude-opus-5-5"        # $4 in / $20 out / $0.20 cache read per M
SONNET = "claude-sonnet-5-5"    # $2 in / $10 out / $0.20 cache read per M
HAIKU = "claude-haiku-5-5"      # $0.10 / $0.50 / $0.01, x5 above 100k prompt tokens
NOW = datetime.now(timezone.utc).replace(microsecond=0)
T0 = NOW - timedelta(hours=2)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def usage(inp=0, out=0, cr=0, c5=0, c1=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": c5 + c1,
            "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1}}


def asst(mid, ts, model, use, tools=()):
    """One assistant transcript line; `tools` = [(tool_use_id, tool_name, input_dict)]."""
    content = [{"type": "tool_use", "id": t, "name": n, "input": i} for t, n, i in tools]
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "uuid": f"u-{mid}-{use['output_tokens']}",
                       "message": {"id": mid, "model": model, "usage": use, "content": content}})


def msg(**kw):
    base = {"input": 0, "output": 0, "cache_read": 0, "c5": 0, "c1": 0}
    base.update(kw)
    return base


class StatsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="router-stats-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.config = self.tmp / "config"
        self.home.mkdir()
        self.projects = self.config / "projects"
        self.projects.mkdir(parents=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "HOME", "TZ")}
        env.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.config))
        env.update(extra)
        return env

    def write_main(self, project, sid, lines):
        path = self.projects / project / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def write_agent(self, project, sid, agent_id, lines, agent_type, tool_use_id=None):
        d = self.projects / project / sid / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        meta = {"agentType": agent_type}
        if tool_use_id:
            meta["toolUseId"] = tool_use_id
        (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def stats(self, *args, **env_extra):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--json", *args], capture_output=True, text=True,
                              env=self.env(**env_extra), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def session(self, report, sid_prefix):
        found = [s for s in report["sessions"] if s["session_id"].startswith(sid_prefix)]
        self.assertEqual(len(found), 1, [s["session_id"] for s in report["sessions"]])
        return found[0]


class TestPricing(StatsTestCase):
    def test_message_cost_matches_a_hand_value(self):
        # 1000 in, 500 out, 10000 cache read, 2000 5m write, 1000 1h write on opus-5-5
        m = msg(input=1000, output=500, cache_read=10000, c5=2000, c1=1000)
        hand = (1000 * 4 + 500 * 20 + 10000 * 0.2 + 2000 * 4 * 1.25 + 1000 * 4 * 2) / 1e6
        self.assertAlmostEqual(hand, 0.034, places=9)
        self.assertAlmostEqual(router_stats.message_cost(OPUS, m), 0.034, places=9)

    def test_long_prompt_multiplier_applies_above_the_threshold_only(self):
        at = msg(input=100, output=100, cache_read=99_900)          # prompt 100000: not above
        over = msg(input=100, output=100, cache_read=100_000)       # prompt 100100: x5
        self.assertAlmostEqual(router_stats.message_cost(HAIKU, at), (100 * 0.1 + 100 * 0.5 + 99_900 * 0.01) / 1e6)
        self.assertAlmostEqual(router_stats.message_cost(HAIKU, over), (100 * 0.5 + 100 * 2.5 + 100_000 * 0.05) / 1e6)

    def test_unknown_model_has_no_price(self):
        self.assertIsNone(router_stats.message_cost("claude-mystery-9", msg(input=1)))

    def test_a_session_costs_the_hand_value(self):
        self.write_main("-home-u-proj", "s1", [asst("m1", T0, OPUS, usage(1000, 500, 10000, 2000, 1000))])
        report = self.stats("--since", "7d")
        s = self.session(report, "s1")
        self.assertAlmostEqual(s["main_cost"], 0.034, places=6)
        self.assertAlmostEqual(report["totals"]["total_cost"], 0.034, places=6)

    def test_missing_cache_breakdown_counts_as_one_hour_writes(self):
        line = json.dumps({"type": "assistant", "timestamp": iso(T0), "message": {
            "id": "m1", "model": OPUS, "usage": {"input_tokens": 0, "output_tokens": 0,
                                                "cache_creation_input_tokens": 1000}, "content": []}})
        path = self.write_main("-home-u-proj", "s1", [line])
        messages, _ = router_stats.read_transcript(path)
        self.assertEqual((messages["m1"]["c5"], messages["m1"]["c1"]), (0, 1000))


class TestDedupe(StatsTestCase):
    def test_largest_output_tokens_line_wins_within_a_file(self):
        # one API message streamed as several lines sharing an id; the final line has the most output
        path = self.write_main("-home-u-proj", "s1", [
            asst("m1", T0, OPUS, usage(10, 5, 1000)),
            asst("m1", T0 + timedelta(seconds=1), OPUS, usage(10, 50, 1000)),
            asst("m1", T0 + timedelta(seconds=2), OPUS, usage(10, 20, 1000)),   # a stale, shorter line
            asst("m2", T0 + timedelta(seconds=3), OPUS, usage(1, 1, 0)),
        ])
        messages, _ = router_stats.read_transcript(path)
        self.assertEqual(sorted(messages), ["m1", "m2"])
        self.assertEqual(messages["m1"]["output"], 50)
        self.assertEqual(messages["m1"]["ts"], T0)  # keeps the first timestamp
        s = self.session(self.stats("--since", "7d"), "s1")
        self.assertEqual(s["main_messages"], 2)
        self.assertAlmostEqual(s["main_cost"], (10 * 4 + 50 * 20 + 1000 * 0.2 + 1 * 4 + 1 * 20) / 1e6, places=6)

    def test_seen_set_drops_ids_across_files_and_collects_new_ones(self):
        a = self.write_main("-home-u-proj", "a", [asst("m1", T0, OPUS, usage(1, 1)), asst("m2", T0, OPUS, usage(1, 1))])
        b = self.write_main("-home-u-proj", "b", [asst("m2", T0, OPUS, usage(1, 1)), asst("m3", T0, OPUS, usage(1, 1))])
        seen = set()
        self.assertEqual(sorted(router_stats.read_transcript(a, seen)[0]), ["m1", "m2"])
        self.assertEqual(sorted(router_stats.read_transcript(b, seen)[0]), ["m3"])
        self.assertEqual(seen, {"m1", "m2", "m3"})

    def test_forked_session_does_not_steal_the_parents_history(self):
        shared = [asst(f"p{i}", T0 + timedelta(minutes=i), OPUS, usage(0, 100)) for i in range(3)]
        # the fork's id sorts first alphabetically, and it copies the parent's history timestamps and all
        parent = self.write_main("-home-u-proj", "zzzz-parent", shared)
        fork = self.write_main("-home-u-proj", "aaaa-fork",
                               shared + [asst("f1", T0 + timedelta(minutes=10), OPUS, usage(0, 100))])
        os.utime(parent, (NOW.timestamp() - 7200, NOW.timestamp() - 7200))   # the parent was written earlier
        os.utime(fork, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
        report = self.stats("--since", "7d")
        self.assertEqual(self.session(report, "zzzz-parent")["main_messages"], 3)
        self.assertEqual(self.session(report, "aaaa-fork")["main_messages"], 1)
        self.assertAlmostEqual(report["totals"]["total_cost"], 4 * 100 * 20 / 1e6, places=6)

    def test_session_that_started_earlier_is_processed_first(self):
        early = [asst("e1", T0, OPUS, usage(0, 100)), asst("shared", T0 + timedelta(minutes=5), OPUS, usage(0, 100))]
        late = [asst("shared", T0 + timedelta(minutes=5), OPUS, usage(0, 100)),
                asst("l1", T0 + timedelta(minutes=9), OPUS, usage(0, 100))]
        self.write_main("-home-u-proj", "aaaa-late", late)
        self.write_main("-home-u-proj", "zzzz-early", early)
        report = self.stats("--since", "7d")
        self.assertEqual(self.session(report, "zzzz-early")["main_messages"], 2)
        self.assertEqual(self.session(report, "aaaa-late")["main_messages"], 1)

    def test_a_copied_subagent_transcript_is_counted_once(self):
        main = [asst("mp", T0, OPUS, usage(10, 10, 5000), [("toolu_1", "Agent", {"subagent_type": "scout"})])]
        sub = [asst("s1", T0 + timedelta(seconds=5), HAIKU, usage(10, 100, 2000))]
        self.write_main("-home-u-proj", "aaaa-parent", main)
        self.write_agent("-home-u-proj", "aaaa-parent", "x1", sub, "scout", "toolu_1")
        os.utime(self.projects / "-home-u-proj" / "aaaa-parent.jsonl", (NOW.timestamp() - 7200,) * 2)
        self.write_main("-home-u-proj", "zzzz-fork", main)
        self.write_agent("-home-u-proj", "zzzz-fork", "x1", sub, "scout", "toolu_1")
        report = self.stats("--since", "7d")
        self.assertEqual(report["totals"]["subagents_by_type"]["scout"]["runs"], 1)
        self.assertEqual(report["totals"]["subagents_by_type"]["scout"]["messages"], 1)
        self.assertEqual(report["totals"]["delegation_count"], 1)


class TestRouterAgents(StatsTestCase):
    def test_normalize_agent(self):
        n = router_stats.normalize_agent
        self.assertEqual(n("model-router:scout"), "scout")
        self.assertEqual(n("some-plugin:reviewer"), "reviewer")
        self.assertEqual(n("a:b:builder"), "builder")
        self.assertEqual(n("scout"), "scout")
        self.assertEqual(n("general-purpose"), "general-purpose")
        self.assertEqual(router_stats.ROUTER_AGENTS, {"scout", "mechanic", "builder", "test-writer", "reviewer"})

    def make_session(self):
        main = [
            asst("mp", T0, OPUS, usage(10, 100, 99_990),
                 [("toolu_scout", "Agent", {"subagent_type": "model-router:scout"}),
                  ("toolu_guide", "Task", {"subagent_type": "claude-code-guide"})]),
            asst("m2", T0 + timedelta(minutes=5), OPUS, usage(10, 100, 100_000)),
        ]
        self.write_main("-home-u-proj", "s1", main)
        sub = [asst("a1", T0 + timedelta(seconds=10), HAIKU, usage(100, 400, 3000, 0, 900))]
        self.write_agent("-home-u-proj", "s1", "scout1", sub, "model-router:scout", "toolu_scout")
        sub2 = [asst("b1", T0 + timedelta(seconds=11), HAIKU, usage(100, 400, 3000, 0, 900))]
        self.write_agent("-home-u-proj", "s1", "guide1", sub2, "claude-code-guide", "toolu_guide")
        return self.session(self.stats("--since", "7d"), "s1")

    def test_non_router_agent_costs_but_does_not_save(self):
        s = self.make_session()
        types = s["subagents"]
        self.assertEqual(sorted(types), ["claude-code-guide", "scout"])  # plugin prefix normalized
        self.assertTrue(types["scout"]["router"])
        self.assertFalse(types["claude-code-guide"]["router"])
        self.assertAlmostEqual(types["claude-code-guide"]["cost"], types["scout"]["cost"], places=9)
        self.assertAlmostEqual(s["subagent_cost"], 2 * types["scout"]["cost"], places=9)  # both are spend
        # savings come from the scout run alone: exactly what a lone scout run would give
        self.assertEqual(s["delegations"], {"scout": 1, "claude-code-guide": 1})
        guide_only = router_stats.estimate_saving(OPUS, [(msg(input=100, output=400, cache_read=3000, c1=900),
                                                           s["subagents"]["scout"]["cost"])], 100_000, 1)
        self.assertAlmostEqual(s["saved_inline"], guide_only[0], places=6)
        self.assertAlmostEqual(s["saved_repriced"], guide_only[1], places=6)

    def test_by_type_block_merges_by_normalized_name_and_flags_router_agents(self):
        self.make_session()
        self.write_agent("-home-u-proj", "s1", "scout2",
                         [asst("a2", T0 + timedelta(seconds=20), HAIKU, usage(10, 10, 100))], "scout")
        report = self.stats("--since", "7d")
        by_type = report["totals"]["subagents_by_type"]
        self.assertEqual(by_type["scout"]["runs"], 2)  # "model-router:scout" and "scout" merged
        self.assertTrue(by_type["scout"]["router"])
        self.assertFalse(by_type["claude-code-guide"]["router"])
        text = subprocess.run([sys.executable, str(SCRIPT), "--since", "7d"], capture_output=True, text=True,
                              env=self.env(), timeout=60).stdout
        self.assertRegex(text, r"\n    scout\*\s+claude-haiku-5-5\s+2 runs")
        self.assertRegex(text, r"\n    claude-code-guide\s+claude-haiku-5-5\s+1 run")
        self.assertNotIn("claude-code-guide*", text)

    def test_only_non_router_agents_means_zero_savings(self):
        self.write_main("-home-u-proj", "s1", [asst("mp", T0, OPUS, usage(10, 10, 5000),
                                                    [("toolu_g", "Agent", {"subagent_type": "Explore"})])])
        self.write_agent("-home-u-proj", "s1", "g1", [asst("g1", T0 + timedelta(seconds=3), HAIKU, usage(5, 50, 900))],
                         "Explore", "toolu_g")
        s = self.session(self.stats("--since", "7d"), "s1")
        self.assertGreater(s["subagent_cost"], 0)
        self.assertEqual((s["saved_inline"], s["saved_repriced"]), (0, 0))


class TestSavedEstimate(StatsTestCase):
    """Main opus-5-5 ($4/$20/$0.20), subagent sonnet-5-5 ($2/$10/$0.20); every number below is by hand."""

    S1 = usage(100, 400, 3000, 0, 900)   # ctx 4000
    S2 = usage(50, 200, 4500, 450, 0)    # ctx 5000

    def build(self, sub_usages, tool_use_id="toolu_X", n_after=2, parent_ctx_cr=99_990):
        main = [asst("mp", T0, OPUS, usage(10, 50, parent_ctx_cr), [("toolu_X", "Agent", {"subagent_type": "builder"})])]
        main += [asst(f"later{i}", T0 + timedelta(seconds=100 * (i + 1)), OPUS, usage(1, 1)) for i in range(n_after)]
        self.write_main("-home-u-proj", "s1", main)
        sub = [asst(f"s{i}", T0 + timedelta(seconds=10 * (i + 1)), SONNET, u) for i, u in enumerate(sub_usages)]
        self.write_agent("-home-u-proj", "s1", "b1", sub, "builder", tool_use_id)
        return self.session(self.stats("--since", "7d"), "s1")

    def test_one_subagent_message_parent_found_by_tool_use_id(self):
        # P0 = 10 + 99990 = 100000. inline = (100000*0.2 + 400*20 + 900*4*2)/1e6 = 0.0352; no tail (added = 0)
        # actual = (100*2 + 400*10 + 3000*0.2 + 900*2*2)/1e6 = 0.0084
        s = self.build([self.S1])
        self.assertAlmostEqual(s["subagent_cost"], 0.0084, places=6)
        self.assertAlmostEqual(s["saved_inline"], 0.0352 - 0.0084, places=6)
        # repriced = (100*4 + 400*20 + 3000*0.2 + 900*4*2)/1e6 - actual = 0.0162 - 0.0084
        self.assertAlmostEqual(s["saved_repriced"], 0.0162 - 0.0084, places=6)

    def test_two_messages_add_the_growth_and_the_tail_for_n_after_2(self):
        # step 1 as above (0.0352). step 2: inline_ctx = 100000 + (5000 - 4000) = 101000
        #   (101000*0.2 + 200*20 + 450*4*1.25)/1e6 = 0.02645; tail = 2 later msgs * 1000 added * 0.2/1e6 = 0.0004
        # actual = 0.0084 + (50*2 + 200*10 + 4500*0.2 + 450*2*1.25)/1e6 = 0.0084 + 0.004125
        s = self.build([self.S1, self.S2])
        actual = 0.0084 + 0.004125
        self.assertAlmostEqual(s["subagent_cost"], actual, places=6)
        self.assertAlmostEqual(s["saved_inline"], 0.0352 + 0.02645 + 0.0004 - actual, places=6)
        # repriced: 0.0162 + (50*4 + 200*20 + 4500*0.2 + 450*4*1.25)/1e6 (= 0.00735) - actual
        self.assertAlmostEqual(s["saved_repriced"], 0.0162 + 0.00735 - actual, places=6)

    def test_without_tool_use_id_the_latest_earlier_main_message_is_the_parent(self):
        # same hand values: the parent is the last main message at or before the subagent's first message
        by_id = self.build([self.S1, self.S2])
        by_time = self.build([self.S1, self.S2], tool_use_id=None)
        self.assertAlmostEqual(by_time["saved_inline"], by_id["saved_inline"], places=9)
        self.assertAlmostEqual(by_time["saved_repriced"], by_id["saved_repriced"], places=9)

    def test_unknown_tool_use_id_falls_back_to_time(self):
        by_id = self.build([self.S1, self.S2])
        unknown = self.build([self.S1, self.S2], tool_use_id="toolu_does_not_exist")
        self.assertAlmostEqual(unknown["saved_inline"], by_id["saved_inline"], places=9)

    def test_no_parent_means_zero_parent_context_and_no_tail(self):
        # the subagent starts before any main message: P0 = 0, n_after = 0
        self.write_main("-home-u-proj", "s1", [asst("late", T0 + timedelta(hours=1), OPUS, usage(1, 1, 50_000))])
        self.write_agent("-home-u-proj", "s1", "b1", [asst("s0", T0, SONNET, self.S1), asst("s1", T0 + timedelta(seconds=9), SONNET, self.S2)],
                         "builder", None)
        s = self.session(self.stats("--since", "7d"), "s1")
        # step 1: inline_ctx = 0 -> (400*20 + 900*4*2)/1e6 = 0.0152; step 2: inline_ctx = 1000
        #   (1000*0.2 + 200*20 + 450*4*1.25)/1e6 = 0.00645
        actual = 0.0084 + 0.004125
        self.assertAlmostEqual(s["saved_inline"], 0.0152 + 0.00645 - actual, places=6)

    def test_estimate_saving_long_prompt_rule_uses_the_inline_prompt_size(self):
        # main = haiku-5-5: a 150000-token inline prompt is billed at x5 (cache read 0.05)
        steps = [(msg(input=100, output=400, cache_read=3000, c1=900), 0.0)]
        inline, _ = router_stats.estimate_saving(HAIKU, steps, 150_000, 0)
        self.assertAlmostEqual(inline, (150_000 * 0.05 + 400 * 2.5 + 900 * 0.5 * 2) / 1e6, places=9)

    def test_unknown_main_model_gives_no_estimate(self):
        self.assertEqual(router_stats.estimate_saving("claude-mystery-9", [(msg(input=1), 0.0)], 10, 1), (0.0, 0.0))

    def test_totals_and_percent(self):
        s = self.build([self.S1, self.S2])
        report = self.stats("--since", "7d")
        t = report["totals"]
        self.assertAlmostEqual(t["saved_inline"], s["saved_inline"], places=6)
        self.assertAlmostEqual(t["saved_repriced"], s["saved_repriced"], places=6)
        self.assertNotIn("saved", s)
        expected_pct = 100 * t["saved_inline"] / (t["total_cost"] + t["saved_inline"])
        self.assertAlmostEqual(t["saved_inline_pct"], expected_pct, places=4)
        text = subprocess.run([sys.executable, str(SCRIPT), "--since", "7d"], capture_output=True, text=True,
                              env=self.env(), timeout=60).stdout
        self.assertRegex(text, r"saved \(est\.\)\s+\$[\d.]+\s+\(\d+\.\d% of what it would have cost\)")
        self.assertRegex(text, r"saved \(repriced\)\s+\$[\d.]+\s+floor: ignores the context effect")
        self.assertIn("re-reading the main context at each step", text)


class TestSince(StatsTestCase):
    def test_today_is_local_midnight(self):
        # TZ=XYZ-3 is UTC+3 with no tz database needed
        code = ("import sys, time; sys.path.insert(0, %r); import router_stats; time.tzset();"
                "c = router_stats.parse_since('today'); print(c.isoformat())" % str(ROOT))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=self.env(TZ="XYZ-3"), timeout=30).stdout.strip()
        self.assertRegex(out, r"^\d{4}-\d\d-\d\dT00:00:00\+03:00$")
        local_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date().isoformat()
        self.assertTrue(out.startswith(local_today), (out, local_today))  # not UTC midnight, not tomorrow

    def test_messages_before_local_midnight_are_not_counted(self):
        tz = timezone(timedelta(hours=3))
        midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        self.write_main("-home-u-proj", "s1", [
            asst("before", midnight - timedelta(minutes=1), OPUS, usage(0, 100)),
            asst("after", midnight + timedelta(minutes=1), OPUS, usage(0, 200)),
        ])
        report = self.stats("--since", "today", TZ="XYZ-3")
        self.assertTrue(report["since"].endswith("T00:00:00+03:00"), report["since"])
        s = self.session(report, "s1")
        self.assertEqual(s["main_messages"], 1)
        self.assertAlmostEqual(s["main_cost"], 200 * 20 / 1e6, places=9)

    def test_bad_since_exits_nonzero(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--since", "yesterday-ish"], capture_output=True,
                              text=True, env=self.env(), timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("bad --since", proc.stderr)


class TestBrokenPipe(StatsTestCase):
    def run_with_closed_stdout(self, *args):
        self.write_main("-home-u-proj", "s1", [asst("m1", T0, OPUS, usage(1, 1, 100))])
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--since", "7d", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env())
        proc.stdout.close()  # the reader (`head`) is gone before anything is written
        err = proc.stderr.read().decode()
        proc.stderr.close()
        return proc.wait(timeout=30), err

    def test_closed_pipe_text_output_exits_quietly(self):
        code, err = self.run_with_closed_stdout()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_closed_pipe_json_output_exits_quietly(self):
        code, err = self.run_with_closed_stdout("--json")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_head_in_a_pipeline(self):
        self.write_main("-home-u-proj", "s1", [asst("m1", T0, OPUS, usage(1, 1, 100))])
        proc = subprocess.run(f"{sys.executable} {SCRIPT} --since 7d --json | head -n 1", shell=True,
                              capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(proc.stdout.strip(), "{")
        self.assertEqual(proc.stderr, "")


class TestReadTranscript(StatsTestCase):
    def test_garbage_lines_are_skipped(self):
        path = self.write_main("-home-u-proj", "s1", ["not json", '{"type": "assistant"}', "[1]",
                                                      json.dumps({"type": "assistant", "timestamp": "nope", "message": {}}),
                                                      asst("ok", T0, OPUS, usage(1, 1))])
        messages, tools = router_stats.read_transcript(path)
        self.assertEqual(list(messages), ["ok"])

    def test_missing_file_is_empty(self):
        self.assertEqual(router_stats.read_transcript(self.tmp / "nope.jsonl"), ({}, {}))

    def test_read_meta(self):
        self.write_agent("-home-u-proj", "s1", "a1", [asst("x", T0, HAIKU, usage(1, 1))], "model-router:scout", "toolu_7")
        path = self.projects / "-home-u-proj" / "s1" / "subagents" / "agent-a1.jsonl"
        self.assertEqual(router_stats.read_meta(path), ("model-router:scout", "toolu_7"))
        self.assertEqual(router_stats.read_meta(self.tmp / "agent-zz.jsonl"), ("unknown", None))


if __name__ == "__main__":
    unittest.main()
