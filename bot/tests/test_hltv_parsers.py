from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

BOT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")


def _load_hltv_module() -> ModuleType:
    """Load the pure parser without importing cs2_results/__init__.py.

    Importing the package registers NoneBot handlers and scheduler jobs. Parser
    unit tests intentionally avoid those process-level side effects.
    """
    path = BOT_ROOT / "src/plugins/cs2_results/hltv.py"
    spec = importlib.util.spec_from_file_location("cs2_hltv_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load parser module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class HltvParserHappyPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hltv = _load_hltv_module()

    def test_events_and_whitelist(self) -> None:
        html = _fixture("events_normal.html")

        self.assertEqual(self.hltv.featured_event_ids(html), {"9001"})
        refs = {event.id: event for event in self.hltv.parse_whitelist(html)}
        self.assertEqual(set(refs), {"9001", "9002"})
        self.assertEqual(refs["9001"].name, "Featured Cup")

        events = self.hltv.parse_events(html)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.id, "9002")
        self.assertEqual(event.name, "Major 2026")
        self.assertEqual(event.start_unix, 1_784_563_200_000)
        self.assertEqual(event.end_unix, 1_784_995_200_000)
        self.assertEqual(event.teams, "16")
        self.assertEqual(event.prize, "$1,000,000")
        self.assertEqual(event.location, "Shanghai, China")

    def test_live_and_upcoming_matches(self) -> None:
        html = _fixture("matches_normal.html")
        with patch.object(self.hltv.time, "time", return_value=1_700_000_000):
            live = self.hltv.parse_live_matches(html)
            upcoming = self.hltv.parse_upcoming_matches(html)

        self.assertEqual([match.match_id for match in live], ["1001"])
        self.assertEqual(live[0].event_id, "9002")
        self.assertEqual(live[0].team1, "Alpha")
        self.assertEqual(live[0].team1_logo, "https://cdn.example/teamlogo/alpha-day.png")

        self.assertEqual([match.match_id for match in upcoming], ["1002"])
        self.assertEqual(upcoming[0].best_of, "bo1")
        self.assertEqual(upcoming[0].start_unix, 4_102_444_800_000)
        self.assertEqual(upcoming[0].event_logo, "https://cdn.example/eventlogo/major.png")

    def test_results(self) -> None:
        results = self.hltv.parse_results(_fixture("results_normal.html"))

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.match_id, "999")
        self.assertEqual(result.status, "finished")
        self.assertEqual((result.score1, result.score2), (2, 1))
        self.assertEqual(result.winner, "team1")
        self.assertEqual(result.team2_logo, "https://cdn.example/teamlogo/beta.png")

    def test_match_detail_and_player_stats(self) -> None:
        match = self.hltv.parse_match(
            _fixture("match_detail_normal.html"),
            "https://www.hltv.org/matches/4242/alpha-vs-beta",
        )

        self.assertEqual(match.match_id, "4242")
        self.assertEqual(match.event_id, "9002")
        self.assertEqual(match.event_name, "Major 2026")
        self.assertEqual(match.best_of, 3)
        self.assertTrue(match.is_lan)
        self.assertEqual(match.stage, "Grand Final")
        self.assertEqual((match.team1, match.team2), ("Alpha", "Beta"))
        self.assertEqual(len(match.maps), 1)

        played_map = match.maps[0]
        self.assertTrue(played_map.finished)
        self.assertEqual(played_map.mapstatsid, "777")
        self.assertEqual(played_map.picked_by, "team1")
        self.assertEqual((played_map.team1_score, played_map.team2_score), (13, 8))
        self.assertEqual(played_map.team1_players[0].kd, "20-12")
        self.assertAlmostEqual(played_map.team1_players[0].rating, 1.35)

    def test_vrs_forecast_three_columns_before_the_series_ends(self) -> None:
        match = self.hltv.parse_match(
            _fixture("match_detail_normal.html"),
            "https://www.hltv.org/matches/4242/alpha-vs-beta",
        )

        vrs = match.vrs
        self.assertIsNotNone(vrs)
        self.assertFalse(vrs.settled)  # 赛前:三档预测
        alpha, beta = vrs.pair(match.team1, match.team2)
        self.assertEqual((alpha.name, beta.name), ("Alpha", "Beta"))
        self.assertEqual((alpha.current.points, alpha.current.rank), ("1238pt", 49))
        self.assertFalse(alpha.current.signed)
        self.assertEqual((alpha.win.points, alpha.win.rank, alpha.win.rank_delta), ("+32pt", 44, 5))
        self.assertEqual(alpha.win.trend, "rising")
        self.assertTrue(alpha.win.signed)
        self.assertEqual(
            (alpha.lose.points, alpha.lose.rank, alpha.lose.rank_delta), ("-2pt", 50, -1)
        )
        # 名次不变时 HLTV 不给变化数字
        self.assertEqual((beta.win.points, beta.win.rank, beta.win.rank_delta), ("+2pt", 9, None))
        self.assertEqual(beta.lose.points, "-30pt")

    def test_vrs_result_panel_after_the_series_ends(self) -> None:
        match = self.hltv.parse_match(
            _fixture("match_vrs_result.html"),
            "https://www.hltv.org/matches/4243/alpha-vs-beta",
        )

        vrs = match.vrs
        self.assertIsNotNone(vrs)
        self.assertTrue(vrs.settled)  # 赛后:只剩「赛前 + 实际增减」两列
        # 面板行序与 team1/team2 相反,pair() 必须按队名对齐
        alpha, beta = vrs.pair(match.team1, match.team2)
        self.assertEqual((alpha.name, beta.name), ("Alpha", "Beta"))
        self.assertEqual((alpha.current.points, alpha.current.rank), ("1242pt", 48))
        self.assertEqual((alpha.win.points, alpha.win.rank, alpha.win.rank_delta), ("+41pt", 41, 7))
        self.assertIsNone(alpha.lose)
        self.assertEqual((beta.current.points, beta.current.rank), ("2012pt", 2))
        self.assertEqual((beta.win.points, beta.win.rank), ("-31pt", 2))
        self.assertIsNone(beta.lose)


    def test_bracket_round_ignores_scheduled_shells_until_opponents_exist(self) -> None:
        hltv = self.hltv
        self.assertTrue(hltv._requires_opponent_selection("BLAST Bounty 2026 Season 2 Finals"))
        self.assertFalse(hltv._requires_opponent_selection("IEM Cologne 2026"))


        scheduled_shell = hltv.Matchup(
            "2396019",
            "",
            1_785_580_200_000,
            3,
            hltv.SlotTeam(),
            hltv.SlotTeam(),
        )
        inferred_pairing = hltv.Matchup(
            None,
            "",
            0,
            0,
            hltv.SlotTeam("Spirit"),
            hltv.SlotTeam("MOUZ"),
        )
        confirmed_match = hltv.Matchup(
            "2396018",
            "",
            1_785_416_400_000,
            3,
            hltv.SlotTeam("Liquid"),
            hltv.SlotTeam("Spirit"),
        )

        self.assertFalse(hltv.BracketRound("半决赛", [scheduled_shell]).is_pending())
        self.assertTrue(
            hltv.BracketRound(
                "半决赛", [scheduled_shell], requires_opponent_selection=True
            ).is_pending()
        )
        self.assertTrue(
            hltv.BracketRound(
                "半决赛", [inferred_pairing], requires_opponent_selection=True
            ).is_pending()
        )
        self.assertFalse(
            hltv.BracketRound(
                "八强赛", [confirmed_match], requires_opponent_selection=True
            ).is_pending()
        )

        semifinal = hltv.BracketRound(
            "半决赛",
            [inferred_pairing],
            stage="semifinal",
            requires_opponent_selection=True,
        )
        bracket = hltv.Bracket("single", "Stage 1", upper=[semifinal])
        reserved_slot = hltv._SchedEntry(
            start=1_785_580_200_000,
            bo=3,
            stage="semifinal",
            teams=frozenset(),
        )
        hltv._enrich_bracket_schedule([bracket], [reserved_slot])
        self.assertEqual((inferred_pairing.start_unix, inferred_pairing.best_of), (0, 0))
        self.assertTrue(semifinal.is_pending())

        official_match = hltv._SchedEntry(
            start=1_785_580_200_000,
            bo=3,
            stage="semifinal",
            teams=frozenset({"spirit", "mouz"}),
        )
        hltv._enrich_bracket_schedule([bracket], [official_match])
        self.assertEqual(
            (inferred_pairing.start_unix, inferred_pairing.best_of),
            (1_785_580_200_000, 3),
        )
        self.assertFalse(semifinal.is_pending())


        normal_pairing = hltv.Matchup(
            None,
            "",
            0,
            0,
            hltv.SlotTeam("Alpha"),
            hltv.SlotTeam("Beta"),
        )
        normal_round = hltv.BracketRound(
            "半决赛", [normal_pairing], stage="semifinal"
        )
        normal_bracket = hltv.Bracket("single", "Playoffs", upper=[normal_round])
        hltv._enrich_bracket_schedule([normal_bracket], [reserved_slot])
        self.assertEqual(
            (normal_pairing.start_unix, normal_pairing.best_of),
            (1_785_580_200_000, 3),
        )
        self.assertFalse(normal_round.is_pending())

    def test_multi_tree_same_stage_backfill_stays_in_its_own_tree(self) -> None:
        """EWC 形态:主单败树与小组双败树都有 grandfinal 轮,主赛段总决赛的排期
        不得回填到小组树的同名空轮上(回归:小组总决赛曾显示主赛段的时间/BO5)。"""
        hltv = self.hltv

        def _mu(t1: str = "", t2: str = "") -> object:
            return hltv.Matchup(None, "", 0, 0, hltv.SlotTeam(t1), hltv.SlotTeam(t2))

        def _build() -> tuple:
            main_final = _mu()
            main = hltv.Bracket(
                "single",
                "Playoffs",
                upper=[
                    hltv.BracketRound(
                        "半决赛", [_mu("FaZe", "Vitality"), _mu("B8", "Spirit")], stage="semifinal"
                    ),
                    hltv.BracketRound("总决赛", [main_final], stage="grandfinal"),
                ],
            )
            group_final = _mu()
            group = hltv.Bracket(
                "double",
                "Group A",
                upper=[hltv.BracketRound("胜者组首轮", [_mu("Spirit", "JiJieHao")], stage="upperquarter")],
                finals=[hltv.BracketRound("总决赛", [group_final], stage="grandfinal")],
            )
            return main, group, main_final, group_final

        # 场次带队名:两队都只同属主树 → 只回填主树总决赛,小组树保持待定
        main, group, main_final, group_final = _build()
        named_final = hltv._SchedEntry(
            start=1_787_484_600_000, bo=5, stage="grandfinal", teams=frozenset({"faze", "spirit"})
        )
        hltv._enrich_bracket_schedule([main, group], [named_final])
        self.assertEqual((main_final.start_unix, main_final.best_of), (1_787_484_600_000, 5))
        self.assertEqual((group_final.start_unix, group_final.best_of), (0, 0))

        # 场次双方待定:多棵树的同名轮次无法归属 → 谁都不回填,而不是错发给先遍历的树
        main, group, main_final, group_final = _build()
        tbd_final = hltv._SchedEntry(
            start=1_787_484_600_000, bo=5, stage="grandfinal", teams=frozenset()
        )
        hltv._enrich_bracket_schedule([main, group], [tbd_final])
        self.assertEqual((main_final.start_unix, main_final.best_of), (0, 0))
        self.assertEqual((group_final.start_unix, group_final.best_of), (0, 0))


class HltvParserDegradedInputContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hltv = _load_hltv_module()

    def test_missing_optional_fields_use_defaults_or_skip_incomplete_rows(self) -> None:
        html = _fixture("fields_missing.html")

        events = self.hltv.parse_events(html)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "Minimal Event")
        self.assertEqual((events[0].start_unix, events[0].end_unix), (0, 0))
        self.assertEqual((events[0].date_text, events[0].teams, events[0].prize), ("", "", ""))

        self.assertEqual(self.hltv.parse_results(html), [])

        match = self.hltv.parse_match(html, "https://www.hltv.org/matches/6/minimal")
        self.assertEqual((match.team1, match.team2), ("Team 1", "Team 2"))
        self.assertEqual(len(match.maps), 1)
        self.assertEqual(match.maps[0].name, "Ancient")
        self.assertFalse(match.maps[0].finished)
        self.assertIsNone(match.maps[0].mapstatsid)
        self.assertIsNone(match.vrs)  # 无 VRS 模块 → 卡片自动不显示该面板

    def test_cloudflare_page_never_becomes_domain_data(self) -> None:
        html = _fixture("cloudflare_challenge.html")

        self.assertEqual(self.hltv.parse_whitelist(html), [])
        self.assertEqual(self.hltv.featured_event_ids(html), set())
        self.assertEqual(self.hltv.parse_events(html), [])
        self.assertEqual(self.hltv.parse_live_matches(html), [])
        self.assertEqual(self.hltv.parse_upcoming_matches(html), [])
        self.assertEqual(self.hltv.parse_results(html), [])

        match = self.hltv.parse_match(html, "https://www.hltv.org/matches/7/blocked")
        self.assertEqual(match.match_id, "7")
        self.assertEqual((match.team1, match.team2), ("Team 1", "Team 2"))
        self.assertEqual(match.maps, [])
        self.assertEqual(match.event_name, "")
        self.assertIsNone(match.vrs)

        schedule = self.hltv.parse_event_schedule(html, "9002")
        self.assertEqual(schedule.event_id, "9002")
        self.assertEqual(schedule.name, "")
        self.assertIsNone(schedule.swiss)
        self.assertEqual(schedule.brackets, [])
        self.assertEqual(schedule.groups, [])


if __name__ == "__main__":
    unittest.main()
