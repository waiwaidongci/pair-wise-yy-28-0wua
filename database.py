from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from continuity import find_issues


class DomainError(ValueError):
    """Business rule violation."""


ELEMENT_KINDS = {"character", "costume", "prop", "injury"}
RULES = {"stable", "monotonic", "allowed"}


class ContinuityDB:
    """Non-linear film continuity checker with reviewable corrections."""

    def __init__(self, path: str = "continuity.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('producer','continuity','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS productions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shoot_days (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              day_code TEXT NOT NULL,
              shoot_date TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(production_id,day_code)
            );
            CREATE TABLE IF NOT EXISTS scenes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              scene_number TEXT NOT NULL,
              title TEXT NOT NULL,
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              shoot_day_id INTEGER REFERENCES shoot_days(id) ON DELETE SET NULL,
              day_order INTEGER,
              released INTEGER NOT NULL DEFAULT 0 CHECK(released IN (0,1)),
              UNIQUE(production_id,scene_number),
              UNIQUE(production_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS shots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              shot_code TEXT NOT NULL,
              shoot_order INTEGER NOT NULL CHECK(shoot_order > 0),
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','locked')),
              version INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(scene_id,shot_code),
              UNIQUE(scene_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS elements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('character','costume','prop','injury')),
              rule TEXT NOT NULL CHECK(rule IN ('stable','monotonic','allowed')),
              description TEXT NOT NULL DEFAULT '',
              UNIQUE(production_id,name)
            );
            CREATE TABLE IF NOT EXISTS element_transitions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_state TEXT NOT NULL,
              to_state TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              UNIQUE(element_id,from_state,to_state)
            );
            CREATE TABLE IF NOT EXISTS element_states (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              state_value TEXT NOT NULL,
              numeric_value REAL,
              note TEXT NOT NULL DEFAULT '',
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(shot_id,element_id)
            );
            CREATE TABLE IF NOT EXISTS conflicts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_shot_id INTEGER NOT NULL REFERENCES shots(id),
              to_shot_id INTEGER NOT NULL REFERENCES shots(id),
              scope TEXT NOT NULL DEFAULT 'within' CHECK(scope IN ('within','cross')),
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','exempted','resolved')),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              fingerprint TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adjustment_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              shot_id INTEGER NOT NULL REFERENCES shots(id),
              element_id INTEGER NOT NULL REFERENCES elements(id),
              new_value TEXT NOT NULL,
              numeric_value REAL,
              reason TEXT NOT NULL,
              proposed_by INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
              reviewed_by INTEGER REFERENCES users(id),
              review_note TEXT NOT NULL DEFAULT '',
              proposed_at TEXT NOT NULL,
              reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS exemptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              reason TEXT NOT NULL,
              approved_by INTEGER NOT NULL REFERENCES users(id),
              approved_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """为已存在的旧数据库补齐拍摄日、放行、跨场范围等列。"""
        scene_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(scenes)").fetchall()}
        if "shoot_day_id" not in scene_cols:
            self.conn.execute("ALTER TABLE scenes ADD COLUMN shoot_day_id INTEGER REFERENCES shoot_days(id) ON DELETE SET NULL")
        if "day_order" not in scene_cols:
            self.conn.execute("ALTER TABLE scenes ADD COLUMN day_order INTEGER")
        if "released" not in scene_cols:
            self.conn.execute("ALTER TABLE scenes ADD COLUMN released INTEGER NOT NULL DEFAULT 0")
        conflict_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(conflicts)").fetchall()}
        if "scope" not in conflict_cols:
            self.conn.execute("ALTER TABLE conflicts ADD COLUMN scope TEXT NOT NULL DEFAULT 'within'")
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        producer = self.add_user("制片", "producer")
        continuity = self.add_user("场记", "continuity")
        self.add_user("审片", "reviewer")
        production = self.create_production("雨夜追踪", "非线性拍摄出的连续性示例", producer)
        # 叙事顺序：S01 初伤 -> S02 伤势加重；但拍摄顺序相反，场记按现场记录的
        # 伤势被带偏，S01 记成“重度”、S02 反而是“轻度”，形成跨场回退冲突。
        s01_scene = self.add_scene(production, "S01", "巷口初遇", 1)
        s02_scene = self.add_scene(production, "S02", "雨夜追踪", 2)
        s01 = self.add_shot(s01_scene, "S01-01", 2, 1, "现场先拍的重伤戏", continuity)
        s02 = self.add_shot(s02_scene, "S02-01", 1, 1, "后拍的初伤戏", continuity)
        injury = self.add_element(production, "主角左臂伤痕", "injury", "monotonic", "伤痕严重程度只能递增")
        self.set_element_state(s01, injury, "重度", 3, "", continuity)
        self.set_element_state(s02, injury, "轻度", 1, "", continuity)
        # 拍摄日 Day1：先排叙事靠前的 S01（无阻断），S02 因跨场衔接问题被挡下
        day = self.add_shoot_day(production, "Day1", "2026-09-25", "雨夜外景", producer)
        self.schedule_scene(s01_scene, producer, day, 2)
        self.check_production(production)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"producer", "continuity", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_production(self, title: str, description: str, user_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["role"] != "producer" or not title.strip():
            raise DomainError("只有制片人可以创建项目")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO productions(title,description,created_by,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), user_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def _production_for_user(self, production_id: int, user_id: int) -> sqlite3.Row:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise DomainError("用户不存在")
        if user["role"] == "reviewer":
            raise DomainError("审片人员只能审核方案和豁免，不能直接编排")
        return user

    def add_scene(self, production_id: int, scene_number: str, title: str, narrative_order: int) -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not scene_number.strip() or not title.strip() or narrative_order <= 0:
            raise DomainError("场次参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO scenes(production_id,scene_number,title,narrative_order) VALUES(?,?,?,?)",
                    (production_id, scene_number.strip(), title.strip(), narrative_order),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_shot(self, scene_id: int, shot_code: str, shoot_order: int, narrative_order: int,
                 description: str, user_id: int) -> int:
        scene = self.conn.execute("SELECT production_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        user = self._production_for_user(scene["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权创建镜头")
        if not shot_code.strip() or shoot_order <= 0 or narrative_order <= 0:
            raise DomainError("镜头参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO shots(scene_id,shot_code,shoot_order,narrative_order,description,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (scene_id, shot_code.strip(), shoot_order, narrative_order, description.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次内镜头编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_element(self, production_id: int, name: str, kind: str, rule: str, description: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not name.strip() or kind not in ELEMENT_KINDS or rule not in RULES:
            raise DomainError("连续性元素参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO elements(production_id,name,kind,rule,description) VALUES(?,?,?,?,?)",
                    (production_id, name.strip(), kind, rule, description.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目内元素名称不能重复") from exc
        return int(cur.lastrowid)

    def add_transition(self, element_id: int, from_state: str, to_state: str, note: str = "") -> int:
        element = self.conn.execute("SELECT rule FROM elements WHERE id=?", (element_id,)).fetchone()
        if not element or element["rule"] != "allowed":
            raise DomainError("只有 allowed 规则元素需要配置状态转移")
        if not from_state.strip() or not to_state.strip() or from_state == to_state:
            raise DomainError("状态转移必须包含两个不同状态")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO element_transitions(element_id,from_state,to_state,note) VALUES(?,?,?,?)",
                    (element_id, from_state.strip(), to_state.strip(), note.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该状态转移已存在") from exc
        return int(cur.lastrowid)

    def set_element_state(self, shot_id: int, element_id: int, state_value: str, numeric_value: float | None,
                          note: str, user_id: int) -> dict:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (element_id,)).fetchone()
        if not shot or not element or shot["production_id"] != element["production_id"]:
            raise DomainError("镜头与元素不属于同一项目")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权修改连续性状态")
        if shot["status"] == "locked":
            raise DomainError("镜头已锁定，不能直接修改状态")
        if not state_value.strip():
            raise DomainError("状态值不能为空")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则必须提供 numeric_value")
        with self.transaction():
            try:
                self.conn.execute(
                    "INSERT INTO element_states(shot_id,element_id,state_value,numeric_value,note,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (shot_id, element_id, state_value.strip(), numeric_value, note.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                self.conn.execute(
                    "UPDATE element_states SET state_value=?,numeric_value=?,note=?,updated_by=?,updated_at=? WHERE shot_id=? AND element_id=?",
                    (state_value.strip(), numeric_value, note.strip(), user_id, datetime.now().isoformat(), shot_id, element_id),
                )
            self.conn.execute("UPDATE shots SET version=version+1,updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), shot_id))
            self._sync_production(shot["production_id"])
        return {"shot_id": shot_id, "element_id": element_id, "conflicts": self.list_conflicts(shot["scene_id"])}

    # ---- 连续性检测（逻辑在 continuity.py，这里负责取数与落库） ----

    def _production_data(self, production_id: int):
        scenes = [dict(r) for r in self.conn.execute(
            "SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)
        ).fetchall()]
        shots = [dict(r) for r in self.conn.execute(
            "SELECT s.* FROM shots s JOIN scenes sc ON sc.id=s.scene_id "
            "WHERE sc.production_id=? ORDER BY s.narrative_order", (production_id,)
        ).fetchall()]
        elements = [dict(r) for r in self.conn.execute(
            "SELECT * FROM elements WHERE production_id=? ORDER BY id", (production_id,)
        ).fetchall()]
        states = {
            (r["shot_id"], r["element_id"]): dict(r)
            for r in self.conn.execute(
                "SELECT st.* FROM element_states st JOIN shots s ON s.id=st.shot_id "
                "JOIN scenes sc ON sc.id=s.scene_id WHERE sc.production_id=?", (production_id,)
            ).fetchall()
        }
        transitions = {
            (r["element_id"], r["from_state"], r["to_state"])
            for r in self.conn.execute(
                "SELECT t.element_id,t.from_state,t.to_state FROM element_transitions t "
                "JOIN elements e ON e.id=t.element_id WHERE e.production_id=?", (production_id,)
            ).fetchall()
        }
        return scenes, shots, elements, states, transitions

    def _detect_production(self, production_id: int) -> list[dict]:
        scenes, shots, elements, states, transitions = self._production_data(production_id)
        return find_issues(scenes, elements, shots, states, transitions)

    def _sync_production(self, production_id: int) -> None:
        """整片重检：同步单场内与跨场衔接冲突，必须在事务内调用。"""
        detected = self._detect_production(production_id)
        active_fingerprints = {issue["fingerprint"] for issue in detected}
        existing_rows = self.conn.execute(
            "SELECT c.* FROM conflicts c JOIN scenes sc ON sc.id=c.scene_id WHERE sc.production_id=?",
            (production_id,),
        ).fetchall()
        for row in existing_rows:
            if row["active"] and row["fingerprint"] not in active_fingerprints:
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), row["id"]),
                )
        now = datetime.now().isoformat()
        for issue in detected:
            existing = self.conn.execute("SELECT * FROM conflicts WHERE fingerprint=?", (issue["fingerprint"],)).fetchone()
            if existing:
                status = "exempted" if existing["status"] == "exempted" else "open"
                self.conn.execute(
                    "UPDATE conflicts SET active=1,status=?,scope=?,scene_id=?,detail=?,updated_at=? WHERE id=?",
                    (status, issue["scope"], issue["scene_id"], issue["detail"], now, existing["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO conflicts(scene_id,element_id,from_shot_id,to_shot_id,scope,kind,detail,status,active,fingerprint,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?, 'open',1,?,?,?)",
                    (issue["scene_id"], issue["element_id"], issue["from_shot_id"], issue["to_shot_id"],
                     issue["scope"], issue["kind"], issue["detail"], issue["fingerprint"], now, now),
                )

    def check_scene(self, scene_id: int) -> list[dict]:
        scene = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        with self.transaction():
            self._sync_production(scene["production_id"])
        return self.list_conflicts(scene_id)

    def check_production(self, production_id: int) -> dict:
        """整片预检：按叙事顺序重查单场与跨场衔接，返回每个场次的待处理问题与放行状态。"""
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        with self.transaction():
            self._sync_production(production_id)
        return self._production_view(production_id)

    def _blocking_conflicts(self, scene_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.* FROM conflicts c WHERE c.scene_id=? AND c.active=1 AND c.status!='exempted' "
            "ORDER BY c.scope,c.id", (scene_id,)
        ).fetchall()

    def _release_gate(self, scene_id: int) -> list[str]:
        """返回阻断原因列表（含单场与跨场衔接）；为空表示可以放行/排期。"""
        reasons = []
        for conflict in self._blocking_conflicts(scene_id):
            if conflict["scope"] == "cross":
                reasons.append(f"跨场衔接问题待处理（冲突 #{conflict['id']}）：{conflict['detail']}")
            else:
                reasons.append(f"单场连续性问题待处理（冲突 #{conflict['id']}）：{conflict['detail']}")
        return reasons

    # ---- 拍摄日、排期与放行（制片/场记维护） ----

    def add_shoot_day(self, production_id: int, day_code: str, shoot_date: str,
                      note: str, user_id: int) -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        user = self._production_for_user(production_id, user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("只有制片或场记可以维护拍摄日")
        if not day_code.strip() or not shoot_date.strip():
            raise DomainError("拍摄日编号和日期必须填写")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO shoot_days(production_id,day_code,shoot_date,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (production_id, day_code.strip(), shoot_date.strip(), note.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("拍摄日编号已存在") from exc
        return int(cur.lastrowid)

    def _get_scene_for_edit(self, scene_id: int, user_id: int) -> sqlite3.Row:
        scene = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        user = self._production_for_user(scene["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("只有制片或场记可以维护排期和放行")
        return scene

    def schedule_scene(self, scene_id: int, user_id: int, shoot_day_id: int | None = None,
                       day_order: int | None = None) -> dict:
        """把场次排进拍摄日；存在未处理（未豁免）的单场或跨场问题时拒绝排期。"""
        scene = self._get_scene_for_edit(scene_id, user_id)
        if shoot_day_id is not None:
            day = self.conn.execute(
                "SELECT * FROM shoot_days WHERE id=? AND production_id=?", (shoot_day_id, scene["production_id"])
            ).fetchone()
            if not day:
                raise DomainError("拍摄日不存在")
            if day_order is None or day_order <= 0:
                raise DomainError("必须给出场次在拍摄日内的拍摄顺序")
        with self.transaction():
            self._sync_production(scene["production_id"])
            blockers = self._release_gate(scene_id)
            if blockers:
                raise DomainError("存在未处理的连续性问题，不能排期：" + "；".join(blockers))
            self.conn.execute(
                "UPDATE scenes SET shoot_day_id=?,day_order=? WHERE id=?",
                (shoot_day_id, day_order, scene_id),
            )
        return {"scene_id": scene_id, "shoot_day_id": shoot_day_id, "day_order": day_order,
                "blocked_reasons": []}

    def release_scene(self, scene_id: int, user_id: int) -> dict:
        """整片预检后放行场次；未处理问题未清时不能放行。"""
        scene = self._get_scene_for_edit(scene_id, user_id)
        with self.transaction():
            self._sync_production(scene["production_id"])
            blockers = self._release_gate(scene_id)
            if blockers:
                raise DomainError("存在未处理的连续性问题，不能放行：" + "；".join(blockers))
            self.conn.execute("UPDATE scenes SET released=1 WHERE id=?", (scene_id,))
        return {"scene_id": scene_id, "released": 1, "blocked_reasons": []}

    def unrelease_scene(self, scene_id: int, user_id: int) -> dict:
        self._get_scene_for_edit(scene_id, user_id)
        with self.transaction():
            self.conn.execute("UPDATE scenes SET released=0 WHERE id=?", (scene_id,))
        return {"scene_id": scene_id, "released": 0}

    def _scene_view(self, scene: sqlite3.Row) -> dict:
        blockers = self._release_gate(scene["id"])
        if scene["released"] and not blockers:
            status = "released"
        elif blockers:
            status = "blocked"
        else:
            status = "ready"
        return {
            **dict(scene),
            "status": status,
            "blocked_reasons": blockers,
            "conflicts": self.list_conflicts(scene["id"], include_resolved=True),
        }

    def _production_view(self, production_id: int) -> dict:
        scenes = [self._scene_view(r) for r in self.conn.execute(
            "SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)
        ).fetchall()]
        scene_by_id = {s["id"]: s for s in scenes}
        days = []
        for day in self.conn.execute(
            "SELECT * FROM shoot_days WHERE production_id=? ORDER BY shoot_date,id", (production_id,)
        ).fetchall():
            day_scenes = [s for s in scenes if s["shoot_day_id"] == day["id"]]
            day_scenes.sort(key=lambda s: (s["day_order"] if s["day_order"] is not None else 1 << 30, s["id"]))
            if any(s["status"] == "blocked" for s in day_scenes):
                day_status = "blocked"
            elif day_scenes and all(s["status"] == "released" for s in day_scenes):
                day_status = "released"
            elif day_scenes and all(s["status"] in {"released", "ready"} for s in day_scenes):
                day_status = "ready"
            else:
                day_status = "empty"
            days.append({**dict(day), "status": day_status,
                         "scene_ids": [s["id"] for s in day_scenes]})
        unscheduled = [s["id"] for s in scenes if s["shoot_day_id"] is None]
        active = [c for s in scenes for c in s["conflicts"] if c["active"]]
        cross = [c for c in active if c["scope"] == "cross" and c["status"] == "open"]
        return {
            "scenes": scenes,
            "shoot_days": days,
            "unscheduled_scene_ids": unscheduled,
            "open_conflicts": sum(1 for c in active if c["status"] == "open"),
            "open_cross_conflicts": len(cross),
            "exempted_conflicts": sum(1 for c in active if c["status"] == "exempted"),
            "blocked_scene_ids": [s["id"] for s in scenes if s["status"] == "blocked"],
            "released_scene_ids": [s["id"] for s in scenes if s["status"] == "released"],
        }


    def list_conflicts(self, scene_id: int, include_resolved: bool = False) -> list[dict]:
        clause = "" if include_resolved else "AND c.active=1"
        return [dict(row) for row in self.conn.execute(
            "SELECT c.*,e.name AS element_name,"
            "fs.shot_code AS from_shot_code,ts.shot_code AS to_shot_code,"
            "fsc.scene_number AS from_scene_number,tsc.scene_number AS to_scene_number "
            "FROM conflicts c JOIN elements e ON e.id=c.element_id "
            "JOIN shots fs ON fs.id=c.from_shot_id JOIN shots ts ON ts.id=c.to_shot_id "
            "JOIN scenes fsc ON fsc.id=fs.scene_id JOIN scenes tsc ON tsc.id=ts.scene_id "
            f"WHERE c.scene_id=? {clause} ORDER BY c.scope,c.id", (scene_id,)
        ).fetchall()]

    def propose_adjustment(self, conflict_id: int, new_value: str, numeric_value: float | None,
                           reason: str, user_id: int) -> int:
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"]:
            raise DomainError("冲突不存在或已解决")
        if conflict["status"] != "open":
            raise DomainError("已豁免冲突不能提交状态调整方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (conflict["to_shot_id"],)).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (conflict["element_id"],)).fetchone()
        user = self._production_for_user(element["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权提出调整方案")
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        if not new_value.strip() or len(reason.strip()) < 3:
            raise DomainError("新状态和调整理由必须填写")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则调整必须提供 numeric_value")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjustment_plans(conflict_id,shot_id,element_id,new_value,numeric_value,reason,proposed_by,proposed_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (conflict_id, shot["id"], element["id"], new_value.strip(), numeric_value, reason.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该冲突已有调整方案") from exc
        return int(cur.lastrowid)

    def review_adjustment(self, plan_id: int, approve: bool, reviewer_id: int, note: str = "") -> dict:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        if not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("只有审片人可以审核调整方案")
        plan = self.conn.execute("SELECT * FROM adjustment_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan or plan["status"] != "pending":
            raise DomainError("调整方案不存在或已审核")
        if plan["proposed_by"] == reviewer_id:
            raise DomainError("提案人不能审核自己的方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (plan["shot_id"],)).fetchone()
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        with self.transaction():
            status = "approved" if approve else "rejected"
            self.conn.execute(
                "UPDATE adjustment_plans SET status=?,reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?",
                (status, reviewer_id, note.strip(), datetime.now().isoformat(), plan_id),
            )
            if approve:
                try:
                    self.conn.execute(
                        "INSERT INTO element_states(shot_id,element_id,state_value,numeric_value,note,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (plan["shot_id"], plan["element_id"], plan["new_value"], plan["numeric_value"], f"调整方案 #{plan_id}", reviewer_id, datetime.now().isoformat()),
                    )
                except sqlite3.IntegrityError:
                    self.conn.execute(
                        "UPDATE element_states SET state_value=?,numeric_value=?,note=?,updated_by=?,updated_at=? WHERE shot_id=? AND element_id=?",
                        (plan["new_value"], plan["numeric_value"], f"调整方案 #{plan_id}", reviewer_id, datetime.now().isoformat(), plan["shot_id"], plan["element_id"]),
                    )
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), plan["conflict_id"]),
                )
                production_id = self.conn.execute(
                    "SELECT production_id FROM scenes WHERE id=?", (shot["scene_id"],)
                ).fetchone()[0]
                self._sync_production(production_id)
        return {"plan_id": plan_id, "status": status, "conflicts": self.list_conflicts(shot["scene_id"])}

    def approve_exemption(self, conflict_id: int, reason: str, reviewer_id: int) -> int:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"] or not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("冲突或审片人无效")
        if len(reason.strip()) < 8:
            raise DomainError("豁免理由至少8个字符")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO exemptions(conflict_id,reason,approved_by,approved_at) VALUES(?,?,?,?)",
                    (conflict_id, reason.strip(), reviewer_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该冲突已经豁免") from exc
            self.conn.execute("UPDATE conflicts SET status='exempted',updated_at=? WHERE id=?", (datetime.now().isoformat(), conflict_id))
        return int(cur.lastrowid)

    def lock_shot(self, shot_id: int, user_id: int) -> None:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        if not shot:
            raise DomainError("镜头不存在")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权锁定镜头")
        with self.transaction():
            self._sync_production(shot["production_id"])
            blockers = self._release_gate(shot["scene_id"])
            if blockers:
                raise DomainError("场次仍有未处理冲突，不能锁定：" + "；".join(blockers))
            self.conn.execute("UPDATE shots SET status='locked',version=version+1,updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), shot_id))

    def continuity_report(self, production_id: int) -> dict:
        production = self.conn.execute("SELECT * FROM productions WHERE id=?", (production_id,)).fetchone()
        if not production:
            raise DomainError("项目不存在")
        with self.transaction():
            self._sync_production(production_id)
        view = self._production_view(production_id)
        scenes = []
        for scene_view in view["scenes"]:
            shots = [dict(r) for r in self.conn.execute(
                "SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene_view["id"],)
            )]
            scenes.append({**scene_view, "shots": shots})
        return {
            "production": dict(production),
            "elements": [dict(r) for r in self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (production_id,))],
            "scenes": scenes,
            "shoot_days": view["shoot_days"],
            "unscheduled_scene_ids": view["unscheduled_scene_ids"],
            "open_conflicts": view["open_conflicts"],
            "open_cross_conflicts": view["open_cross_conflicts"],
            "exempted_conflicts": view["exempted_conflicts"],
            "blocked_scene_ids": view["blocked_scene_ids"],
            "released_scene_ids": view["released_scene_ids"],
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "productions": [dict(r) for r in self.conn.execute("SELECT * FROM productions ORDER BY id")],
            "shoot_days": [dict(r) for r in self.conn.execute("SELECT * FROM shoot_days ORDER BY production_id,shoot_date,id")],
            "scenes": [dict(r) for r in self.conn.execute("SELECT * FROM scenes ORDER BY production_id,narrative_order")],
            "shots": [dict(r) for r in self.conn.execute("SELECT * FROM shots ORDER BY scene_id,narrative_order")],
        }
