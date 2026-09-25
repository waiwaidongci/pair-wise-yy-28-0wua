import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import ContinuityDB, DomainError


class CrossSceneGateTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = ContinuityDB(self.path)
        self.producer = self.db.add_user("制片", "producer")
        self.continuity = self.db.add_user("场记", "continuity")
        self.reviewer = self.db.add_user("审片", "reviewer")
        self.prod = self.db.create_production("跨场测试", "首尾衔接", self.producer)
        self.sc1 = self.db.add_scene(self.prod, "S01", "第一场", 1)
        self.sc2 = self.db.add_scene(self.prod, "S02", "第二场", 2)
        self.a1 = self.db.add_shot(self.sc1, "S01-01", 1, 1, "尾镜头", self.continuity)
        self.b1 = self.db.add_shot(self.sc2, "S02-01", 1, 1, "首镜头", self.continuity)
        self.injury = self.db.add_element(self.prod, "伤痕", "injury", "monotonic", "只能加重")
        self.db.set_element_state(self.a1, self.injury, "重度", 3, "", self.continuity)
        self.db.set_element_state(self.b1, self.injury, "轻度", 1, "", self.continuity)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _cross_conflict(self):
        view = self.db.check_production(self.prod)
        conflicts = [c for s in view["scenes"] for c in s["conflicts"]
                     if c["active"] and c["scope"] == "cross"]
        self.assertEqual(1, len(conflicts))
        return conflicts[0], view

    def test_cross_conflict_blocks_schedule_and_release(self):
        conflict, view = self._cross_conflict()
        # 跨场问题挂在叙事下游场次 S02 上，S01 不受影响
        self.assertEqual(self.sc2, conflict["scene_id"])
        statuses = {s["id"]: s["status"] for s in view["scenes"]}
        self.assertEqual("ready", statuses[self.sc1])
        self.assertEqual("blocked", statuses[self.sc2])
        day = self.db.add_shoot_day(self.prod, "Day1", "2026-09-25", "", self.producer)
        self.assertEqual({"scene_id": self.sc1, "shoot_day_id": day, "day_order": 1, "blocked_reasons": []},
                         self.db.schedule_scene(self.sc1, self.producer, day, 1))
        with self.assertRaisesRegex(DomainError, "跨场衔接"):
            self.db.schedule_scene(self.sc2, self.producer, day, 2)
        with self.assertRaisesRegex(DomainError, "跨场衔接"):
            self.db.release_scene(self.sc2, self.continuity)
        # 拍摄日因 S02 未排期/阻断不影响已排场次状态展示
        report = self.db.continuity_report(self.prod)
        self.assertIn(self.sc2, report["blocked_scene_ids"])
        self.assertEqual(1, report["open_cross_conflicts"])

    def test_reviewer_cannot_maintain_schedule(self):
        day = self.db.add_shoot_day(self.prod, "Day1", "2026-09-25", "", self.producer)
        with self.assertRaisesRegex(DomainError, "审片"):
            self.db.add_shoot_day(self.prod, "DayX", "2026-09-26", "", self.reviewer)
        with self.assertRaisesRegex(DomainError, "审片"):
            self.db.schedule_scene(self.sc1, self.reviewer, day, 1)
        with self.assertRaisesRegex(DomainError, "审片"):
            self.db.release_scene(self.sc1, self.reviewer)

    def test_exemption_clears_gate_and_state_change_reblocks(self):
        conflict, _ = self._cross_conflict()
        self.db.approve_exemption(conflict["id"], "刻意的闪回叙事安排，允许伤痕回退", self.reviewer)
        day = self.db.add_shoot_day(self.prod, "Day1", "2026-09-25", "", self.continuity)
        self.db.schedule_scene(self.sc2, self.producer, day, 1)
        self.db.release_scene(self.sc2, self.continuity)
        view = self.db.check_production(self.prod)
        self.assertEqual("released", next(s for s in view["scenes"] if s["id"] == self.sc2)["status"])
        self.assertEqual(0, view["open_cross_conflicts"])
        # 放行后再补录一个状态回退的新镜头，产生新的冲突指纹，场次重新被阻断
        b2 = self.db.add_shot(self.sc2, "S02-02", 2, 2, "补录镜头", self.continuity)
        self.db.set_element_state(b2, self.injury, "完好", 0, "", self.continuity)
        view = self.db.check_production(self.prod)
        self.assertEqual("blocked", next(s for s in view["scenes"] if s["id"] == self.sc2)["status"])
        self.assertEqual(1, view["open_conflicts"])

    def test_approved_plan_resolves_cross_conflict_then_gate_passes(self):
        conflict, _ = self._cross_conflict()
        # 跨场问题同样走“制片/场记提案 -> 审片人批准”的调整方案
        plan = self.db.propose_adjustment(conflict["id"], "中度", 3,
                                          "把后场首镜头伤痕调到与前场衔接", self.continuity)
        self.db.review_adjustment(plan, True, self.reviewer, "通过")
        view = self.db.check_production(self.prod)
        self.assertEqual(0, view["open_cross_conflicts"])
        day = self.db.add_shoot_day(self.prod, "Day1", "2026-09-25", "", self.producer)
        self.db.schedule_scene(self.sc2, self.producer, day, 1)
        self.db.release_scene(self.sc2, self.producer)
        view = self.db.check_production(self.prod)
        self.assertEqual("released", next(s for s in view["scenes"] if s["id"] == self.sc2)["status"])

    def test_allowed_rule_governs_cross_boundary(self):
        coat = self.db.add_element(self.prod, "外套", "costume", "allowed", "按剧情穿脱")
        self.db.add_transition(coat, "穿着", "脱下", "进门脱外套")
        self.db.set_element_state(self.a1, coat, "脱下", None, "", self.continuity)
        self.db.set_element_state(self.b1, coat, "穿着", None, "", self.continuity)
        conflict, _ = self._conflict_for(coat)
        self.assertEqual("transition_not_allowed", conflict["kind"])
        self.db.add_transition(coat, "脱下", "穿着", "出门再穿上")
        view = self.db.check_production(self.prod)
        coat_open = [c for s in view["scenes"] for c in s["conflicts"]
                     if c["active"] and c["element_id"] == coat]
        self.assertEqual([], coat_open)

    def test_within_and_cross_conflicts_both_listed(self):
        # 给 S02 再加一个镜头，制造同场回退；跨场与单场问题都要展示、都阻断
        b2 = self.db.add_shot(self.sc2, "S02-02", 2, 2, "更后面的镜头", self.continuity)
        self.db.set_element_state(b2, self.injury, "完好", 0, "", self.continuity)
        view = self.db.check_production(self.prod)
        s2 = next(s for s in view["scenes"] if s["id"] == self.sc2)
        kinds = {c["scope"] for c in s2["conflicts"] if c["active"] and c["status"] == "open"}
        self.assertEqual({"within", "cross"}, kinds)
        self.assertTrue(any("单场" in r for r in s2["blocked_reasons"]))
        self.assertTrue(any("跨场衔接" in r for r in s2["blocked_reasons"]))

    def _conflict_for(self, element):
        view = self.db.check_production(self.prod)
        conflicts = [c for s in view["scenes"] for c in s["conflicts"]
                     if c["active"] and c["scope"] == "cross" and c["element_id"] == element]
        self.assertEqual(1, len(conflicts))
        return conflicts[0], view

    def test_shoot_day_status_aggregates_scenes(self):
        conflict, _ = self._cross_conflict()
        day = self.db.add_shoot_day(self.prod, "Day1", "2026-09-25", "", self.producer)
        self.db.schedule_scene(self.sc1, self.producer, day, 1)
        with self.assertRaises(DomainError):
            self.db.schedule_scene(self.sc2, self.producer, day, 2)
        # S01 无阻断、放行后，拍摄日内全部场次已放行
        self.db.release_scene(self.sc1, self.producer)
        view = self.db.check_production(self.prod)
        d = next(d for d in view["shoot_days"] if d["id"] == day)
        self.assertEqual("released", d["status"])  # 已排且全部放行
        self.assertEqual([self.sc1], d["scene_ids"])
        self.assertEqual([self.sc2], view["unscheduled_scene_ids"])
        # 豁免后 S02 排入同一拍摄日，日状态随全部放行变为 released
        self.db.approve_exemption(conflict["id"], "闪回叙事刻意安排允许回退处理", self.reviewer)
        self.db.schedule_scene(self.sc2, self.continuity, day, 2)
        self.db.release_scene(self.sc2, self.continuity)
        view = self.db.check_production(self.prod)
        d = next(d for d in view["shoot_days"] if d["id"] == day)
        self.assertEqual("released", d["status"])


if __name__ == "__main__":
    unittest.main()
