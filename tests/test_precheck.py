import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import ContinuityDB, DomainError

class PrecheckFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=ContinuityDB(self.path)
        self.producer=self.db.add_user("制片","producer"); self.continuity=self.db.add_user("场记","continuity"); self.reviewer=self.db.add_user("审片","reviewer")
        self.production=self.db.create_production("跨场测试","整片预检与拍摄日放行",self.producer)
        self.scene1=self.db.add_scene(self.production,"S01","巷口",1)
        self.scene2=self.db.add_scene(self.production,"S02","安全屋",2)
        self.a1=self.db.add_shot(self.scene1,"S01-01",1,1,"",self.continuity)
        self.a2=self.db.add_shot(self.scene1,"S01-02",2,2,"",self.continuity)
        self.b1=self.db.add_shot(self.scene2,"S02-01",3,1,"",self.continuity)
        self.jacket=self.db.add_element(self.production,"外套","costume","stable","颜色一致")
        self.db.set_element_state(self.a2,self.jacket,"蓝",None,"",self.continuity)
        self.db.set_element_state(self.b1,self.jacket,"红",None,"",self.continuity)
    def tearDown(self): self.db.close(); os.unlink(self.path)
    def _pending(self): return self.db.run_precheck(self.production)["pending_issues"]
    def test_cross_scene_conflict_detected_by_narrative_order(self):
        pending=self._pending()
        self.assertEqual(1,len(pending))
        issue=pending[0]
        self.assertEqual("cross",issue["scope"]); self.assertEqual("state_changed",issue["kind"])
        self.assertEqual(self.scene2,issue["scene_id"]); self.assertEqual(self.scene1,issue["from_scene_id"])
    def test_monotonic_and_allowed_rules_apply_across_scenes(self):
        injury=self.db.add_element(self.production,"伤痕","injury","monotonic","")
        self.db.set_element_state(self.a2,injury,"中度",2,"",self.continuity)
        self.db.set_element_state(self.b1,injury,"轻度",1,"",self.continuity)
        self.assertIn("regression",{i["kind"] for i in self._pending()})
        lamp=self.db.add_element(self.production,"台灯","prop","allowed","")
        self.db.add_transition(lamp,"亮","灭")
        self.db.set_element_state(self.a2,lamp,"亮",None,"",self.continuity)
        self.db.set_element_state(self.b1,lamp,"灭",None,"",self.continuity)
        self.assertNotIn("transition_not_allowed",{i["kind"] for i in self._pending()})
        self.db.set_element_state(self.b1,lamp,"闪烁",None,"",self.continuity)
        self.assertIn("transition_not_allowed",{i["kind"] for i in self._pending()})
    def test_scheduling_blocked_until_adjustment_approved(self):
        day=self.db.add_shoot_day(self.production,"D1","2026-10-01",self.producer)
        with self.assertRaisesRegex(DomainError,"不能排期"):
            self.db.schedule_scene(day,self.scene2,1,self.continuity)
        with self.assertRaisesRegex(DomainError,"不能排期"):
            self.db.schedule_scene(day,self.scene1,1,self.continuity)
        conflict=self._pending()[0]
        plan=self.db.propose_adjustment(conflict["id"],"蓝",None,"安全屋开场沿用巷口外套颜色",self.continuity)
        self.db.review_adjustment(plan,True,self.reviewer,"同意")
        self.assertEqual([],self._pending())
        self.db.schedule_scene(day,self.scene1,1,self.continuity)
        self.db.schedule_scene(day,self.scene2,2,self.continuity)
        info=self.db.set_shoot_day_release(day,True,self.producer,"可以开拍")
        self.assertEqual("released",info["status"])
    def test_release_blocked_by_new_issue_and_exemption_clears(self):
        day=self.db.add_shoot_day(self.production,"D1","2026-10-01",self.producer)
        self.db.approve_exemption(self._pending()[0]["id"],"导演要求安全屋换红外套表现时间跳跃",self.reviewer)
        self.db.schedule_scene(day,self.scene1,1,self.continuity)
        self.db.schedule_scene(day,self.scene2,2,self.continuity)
        self.assertEqual("released",self.db.set_shoot_day_release(day,True,self.producer,"")["status"])
        injury=self.db.add_element(self.production,"伤痕","injury","monotonic","")
        self.db.set_element_state(self.a2,injury,"中度",2,"",self.continuity)
        self.db.set_element_state(self.b1,injury,"轻度",1,"",self.continuity)
        self.assertTrue(self.db.production_board(self.production)["shoot_days"][0]["needs_recheck"])
        with self.assertRaisesRegex(DomainError,"不能放行"):
            self.db.set_shoot_day_release(day,True,self.producer,"")
        self.db.approve_exemption(self._pending()[0]["id"],"安全屋伤痕回退为闪回段落刻意设计",self.reviewer)
        self.assertEqual("released",self.db.set_shoot_day_release(day,True,self.producer,"复核后放行")["status"])
    def test_empty_day_cannot_be_released(self):
        day=self.db.add_shoot_day(self.production,"D1","2026-10-01",self.producer)
        with self.assertRaisesRegex(DomainError,"还没有排期场次"):
            self.db.set_shoot_day_release(day,True,self.producer,"")
    def test_reviewer_cannot_maintain_schedule(self):
        with self.assertRaisesRegex(DomainError,"审片"):
            self.db.add_shoot_day(self.production,"D1","2026-10-01",self.reviewer)
        day=self.db.add_shoot_day(self.production,"D1","2026-10-01",self.producer)
        with self.assertRaisesRegex(DomainError,"审片"):
            self.db.schedule_scene(day,self.scene1,1,self.reviewer)
        with self.assertRaisesRegex(DomainError,"审片"):
            self.db.set_shoot_day_release(day,True,self.reviewer,"")
    def test_released_day_locked_until_revoked(self):
        day=self.db.add_shoot_day(self.production,"D1","2026-10-01",self.producer)
        self.db.approve_exemption(self._pending()[0]["id"],"红外套为刻意设计",self.reviewer)
        self.db.schedule_scene(day,self.scene1,1,self.continuity)
        self.db.schedule_scene(day,self.scene2,2,self.continuity)
        self.db.set_shoot_day_release(day,True,self.producer,"")
        with self.assertRaisesRegex(DomainError,"已放行"):
            self.db.schedule_scene(day,self.scene2,3,self.continuity)
        with self.assertRaisesRegex(DomainError,"已放行"):
            self.db.unschedule_scene(day,self.scene2,self.continuity)
        self.db.set_shoot_day_release(day,False,self.producer,"")
        self.db.unschedule_scene(day,self.scene2,self.continuity)
        self.db.schedule_scene(day,self.scene1,5,self.continuity)
        board=self.db.production_board(self.production)
        self.assertEqual("draft",board["shoot_days"][0]["status"])
        self.assertEqual(5,board["shoot_days"][0]["scenes"][0]["queue_order"])
        self.assertEqual(1,len(board["shoot_days"][0]["scenes"]))

if __name__=="__main__": unittest.main()
