from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import precheck


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
            CREATE TABLE IF NOT EXISTS scenes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              scene_number TEXT NOT NULL,
              title TEXT NOT NULL,
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
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
              from_scene_id INTEGER REFERENCES scenes(id),
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_shot_id INTEGER NOT NULL REFERENCES shots(id),
              to_shot_id INTEGER NOT NULL REFERENCES shots(id),
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','exempted','resolved')),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              scope TEXT NOT NULL DEFAULT 'scene',
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
            CREATE TABLE IF NOT EXISTS shoot_days (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              shoot_date TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','released')),
              release_note TEXT NOT NULL DEFAULT '',
              released_by INTEGER REFERENCES users(id),
              released_at TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(production_id,label)
            );
            CREATE TABLE IF NOT EXISTS shoot_day_scenes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shoot_day_id INTEGER NOT NULL REFERENCES shoot_days(id) ON DELETE CASCADE,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              queue_order INTEGER NOT NULL CHECK(queue_order > 0),
              added_by INTEGER NOT NULL REFERENCES users(id),
              added_at TEXT NOT NULL,
              UNIQUE(shoot_day_id,scene_id),
              UNIQUE(shoot_day_id,queue_order)
            );
            """
        )
        conflict_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(conflicts)")}
        if "scope" not in conflict_cols:
            self.conn.execute("ALTER TABLE conflicts ADD COLUMN scope TEXT NOT NULL DEFAULT 'scene'")
        if "from_scene_id" not in conflict_cols:
            self.conn.execute("ALTER TABLE conflicts ADD COLUMN from_scene_id INTEGER REFERENCES scenes(id)")
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        producer = self.add_user("制片", "producer")
        continuity = self.add_user("场记", "continuity")
        reviewer = self.add_user("审片", "reviewer")
        production = self.create_production("雨夜追踪", "非线性拍摄出的连续性示例", producer)
        scene = self.add_scene(production, "S01", "巷口相遇", 1)
        s01 = self.add_shot(scene, "S01-01", 2, 1, "角色受伤后", continuity)
        s02 = self.add_shot(scene, "S01-02", 1, 2, "角色尚未受伤", continuity)
        injury = self.add_element(production, "主角左臂伤痕", "injury", "monotonic", "伤痕严重程度只能递增")
        self.set_element_state(s01, injury, "重度", 3, "", continuity)
        self.set_element_state(s02, injury, "轻度", 1, "", continuity)
        scene2 = self.add_scene(production, "S02", "安全屋", 2)
        s03 = self.add_shot(scene2, "S02-01", 3, 1, "逃到安全屋", continuity)
        jacket = self.add_element(production, "主角外套", "costume", "stable", "外套颜色需要跨场一致")
        self.set_element_state(s01, jacket, "蓝色", None, "", continuity)
        self.set_element_state(s02, jacket, "蓝色", None, "", continuity)
        self.set_element_state(s03, jacket, "红色", None, "", continuity)
        self.add_shoot_day(production, "D1 雨夜外景", "2026-10-08", producer)
        self.run_precheck(production)

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
            self._sync_conflicts(shot["scene_id"])
            self._sync_cross_conflicts(shot["production_id"])
        return {"shot_id": shot_id, "element_id": element_id, "conflicts": self.list_conflicts(shot["scene_id"])}

    def _allowed_transitions(self, element_id: int) -> set[tuple[str, str]]:
        return {
            (row["from_state"], row["to_state"])
            for row in self.conn.execute(
                "SELECT from_state,to_state FROM element_transitions WHERE element_id=?", (element_id,)
            ).fetchall()
        }

    def _detect_conflicts(self, scene_id: int) -> list[dict]:
        scene = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        shots = self.conn.execute(
            "SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene_id,)
        ).fetchall()
        elements = self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (scene["production_id"],)).fetchall()
        detected: list[dict] = []
        for element in elements:
            allowed = self._allowed_transitions(element["id"])
            sequence = []
            for shot in shots:
                state = self.conn.execute(
                    "SELECT * FROM element_states WHERE shot_id=? AND element_id=?", (shot["id"], element["id"])
                ).fetchone()
                if state:
                    sequence.append((shot, state))
            for (prev_shot, prev), (shot, current) in zip(sequence, sequence[1:]):
                kind, detail = precheck.evaluate_transition(element, prev, current, allowed)
                if kind:
                    fingerprint = f"{scene_id}:{element['id']}:{prev_shot['id']}:{shot['id']}:{kind}"
                    detected.append({
                        "scene_id": scene_id, "from_scene_id": scene_id, "scope": "scene",
                        "element_id": element["id"], "element_name": element["name"],
                        "from_shot_id": prev_shot["id"], "to_shot_id": shot["id"], "kind": kind,
                        "detail": detail, "fingerprint": fingerprint,
                    })
        return detected

    def _detect_cross_conflicts(self, production_id: int) -> list[dict]:
        scenes = self.conn.execute(
            "SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)
        ).fetchall()
        shots_by_scene: dict[int, list[sqlite3.Row]] = {}
        states_by_shot: dict[tuple[int, int], sqlite3.Row] = {}
        for scene in scenes:
            shots = self.conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene["id"],)).fetchall()
            shots_by_scene[scene["id"]] = shots
            for shot in shots:
                for state in self.conn.execute("SELECT * FROM element_states WHERE shot_id=?", (shot["id"],)).fetchall():
                    states_by_shot[(shot["id"], state["element_id"])] = state
        elements = self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (production_id,)).fetchall()
        transitions = {element["id"]: self._allowed_transitions(element["id"]) for element in elements}
        return precheck.detect_boundary_conflicts(elements, transitions, scenes, shots_by_scene, states_by_shot)

    def _store_detected(self, detected: list[dict], stale_rows: list[sqlite3.Row]) -> None:
        active_fingerprints = {row["fingerprint"] for row in detected}
        now = datetime.now().isoformat()
        for row in stale_rows:
            if row["fingerprint"] not in active_fingerprints:
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
        for issue in detected:
            existing = self.conn.execute("SELECT * FROM conflicts WHERE fingerprint=?", (issue["fingerprint"],)).fetchone()
            if existing:
                status = "exempted" if existing["status"] == "exempted" else "open"
                self.conn.execute(
                    "UPDATE conflicts SET active=1,status=?,detail=?,updated_at=? WHERE id=?",
                    (status, issue["detail"], now, existing["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO conflicts(scene_id,from_scene_id,element_id,from_shot_id,to_shot_id,kind,detail,status,active,scope,fingerprint,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?, 'open',1,?,?,?,?)",
                    (issue["scene_id"], issue["from_scene_id"], issue["element_id"], issue["from_shot_id"], issue["to_shot_id"], issue["kind"], issue["detail"], issue["scope"], issue["fingerprint"], now, now),
                )

    def _sync_conflicts(self, scene_id: int) -> None:
        detected = self._detect_conflicts(scene_id)
        stale = self.conn.execute(
            "SELECT * FROM conflicts WHERE scene_id=? AND active=1 AND scope='scene'", (scene_id,)
        ).fetchall()
        self._store_detected(detected, stale)

    def _sync_cross_conflicts(self, production_id: int) -> None:
        detected = self._detect_cross_conflicts(production_id)
        stale = self.conn.execute(
            "SELECT * FROM conflicts WHERE scope='cross' AND active=1 "
            "AND scene_id IN (SELECT id FROM scenes WHERE production_id=?)", (production_id,)
        ).fetchall()
        self._store_detected(detected, stale)

    def _sync_production(self, production_id: int) -> None:
        for row in self.conn.execute("SELECT id FROM scenes WHERE production_id=?", (production_id,)).fetchall():
            self._sync_conflicts(row["id"])
        self._sync_cross_conflicts(production_id)

    def check_scene(self, scene_id: int) -> list[dict]:
        if not self.conn.execute("SELECT 1 FROM scenes WHERE id=?", (scene_id,)).fetchone():
            raise DomainError("场次不存在")
        with self.transaction():
            self._sync_conflicts(scene_id)
        return self.list_conflicts(scene_id)

    def list_conflicts(self, scene_id: int, include_resolved: bool = False) -> list[dict]:
        clause = "" if include_resolved else "AND c.active=1"
        return [dict(row) for row in self.conn.execute(
            "SELECT c.*,e.name AS element_name,fs.shot_code AS from_shot_code,ts.shot_code AS to_shot_code "
            "FROM conflicts c JOIN elements e ON e.id=c.element_id JOIN shots fs ON fs.id=c.from_shot_id JOIN shots ts ON ts.id=c.to_shot_id "
            f"WHERE c.scene_id=? {clause} ORDER BY c.id", (scene_id,)
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
                self._sync_conflicts(shot["scene_id"])
                production_id = self.conn.execute("SELECT production_id FROM scenes WHERE id=?", (shot["scene_id"],)).fetchone()["production_id"]
                self._sync_cross_conflicts(production_id)
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

    def run_precheck(self, production_id: int) -> dict:
        """整片预检：重检全部场内与跨场冲突，返回看板。"""
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        with self.transaction():
            self._sync_production(production_id)
        return self.production_board(production_id)

    def add_shoot_day(self, production_id: int, label: str, shoot_date: str, user_id: int) -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        self._production_for_user(production_id, user_id)
        if not label.strip():
            raise DomainError("拍摄日名称不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO shoot_days(production_id,label,shoot_date,created_at) VALUES(?,?,?,?)",
                    (production_id, label.strip(), shoot_date.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("拍摄日名称重复") from exc
        return int(cur.lastrowid)

    def schedule_scene(self, shoot_day_id: int, scene_id: int, queue_order: int, user_id: int) -> int:
        day = self.conn.execute("SELECT * FROM shoot_days WHERE id=?", (shoot_day_id,)).fetchone()
        if not day:
            raise DomainError("拍摄日不存在")
        scene = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene or scene["production_id"] != day["production_id"]:
            raise DomainError("场次与拍摄日不属于同一项目")
        self._production_for_user(day["production_id"], user_id)
        if queue_order <= 0:
            raise DomainError("拍摄顺位必须为正整数")
        if day["status"] == "released":
            raise DomainError("拍摄日已放行，调整排期前请先撤销放行")
        with self.transaction():
            self._sync_production(day["production_id"])
            blockers = precheck.blockers_for_scene(self._production_issues(day["production_id"]), scene_id)
            if blockers:
                raise DomainError(f"场次还有 {len(blockers)} 个未处理问题，不能排期：{precheck.blocker_summary(blockers)}")
            existing = self.conn.execute(
                "SELECT * FROM shoot_day_scenes WHERE shoot_day_id=? AND scene_id=?", (shoot_day_id, scene_id)
            ).fetchone()
            try:
                if existing:
                    self.conn.execute(
                        "UPDATE shoot_day_scenes SET queue_order=?,added_by=?,added_at=? WHERE id=?",
                        (queue_order, user_id, datetime.now().isoformat(), existing["id"]),
                    )
                    entry_id = int(existing["id"])
                else:
                    cur = self.conn.execute(
                        "INSERT INTO shoot_day_scenes(shoot_day_id,scene_id,queue_order,added_by,added_at) VALUES(?,?,?,?,?)",
                        (shoot_day_id, scene_id, queue_order, user_id, datetime.now().isoformat()),
                    )
                    entry_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise DomainError("该拍摄顺位已被占用") from exc
        return entry_id

    def unschedule_scene(self, shoot_day_id: int, scene_id: int, user_id: int) -> None:
        day = self.conn.execute("SELECT * FROM shoot_days WHERE id=?", (shoot_day_id,)).fetchone()
        if not day:
            raise DomainError("拍摄日不存在")
        self._production_for_user(day["production_id"], user_id)
        if day["status"] == "released":
            raise DomainError("拍摄日已放行，调整排期前请先撤销放行")
        with self.transaction():
            cur = self.conn.execute(
                "DELETE FROM shoot_day_scenes WHERE shoot_day_id=? AND scene_id=?", (shoot_day_id, scene_id)
            )
            if cur.rowcount == 0:
                raise DomainError("场次不在该拍摄日中")

    def set_shoot_day_release(self, shoot_day_id: int, release: bool, user_id: int, note: str = "") -> dict:
        day = self.conn.execute("SELECT * FROM shoot_days WHERE id=?", (shoot_day_id,)).fetchone()
        if not day:
            raise DomainError("拍摄日不存在")
        self._production_for_user(day["production_id"], user_id)
        with self.transaction():
            if release:
                entries = self.conn.execute(
                    "SELECT scene_id FROM shoot_day_scenes WHERE shoot_day_id=?", (shoot_day_id,)
                ).fetchall()
                if not entries:
                    raise DomainError("拍摄日还没有排期场次，不能放行")
                self._sync_production(day["production_id"])
                blockers = precheck.blockers_for_day(
                    self._production_issues(day["production_id"]), [e["scene_id"] for e in entries]
                )
                if blockers:
                    raise DomainError(f"拍摄日还有 {len(blockers)} 个未处理问题，不能放行：{precheck.blocker_summary(blockers)}")
                self.conn.execute(
                    "UPDATE shoot_days SET status='released',release_note=?,released_by=?,released_at=? WHERE id=?",
                    (note.strip(), user_id, datetime.now().isoformat(), shoot_day_id),
                )
            else:
                self.conn.execute(
                    "UPDATE shoot_days SET status='draft',release_note='',released_by=NULL,released_at=NULL WHERE id=?",
                    (shoot_day_id,),
                )
        return self._day_payload(shoot_day_id)

    def _production_issues(self, production_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT c.*,e.name AS element_name,fs.shot_code AS from_shot_code,ts.shot_code AS to_shot_code,"
            "sfrom.scene_number AS from_scene_number,sto.scene_number AS to_scene_number "
            "FROM conflicts c JOIN elements e ON e.id=c.element_id "
            "JOIN shots fs ON fs.id=c.from_shot_id JOIN shots ts ON ts.id=c.to_shot_id "
            "JOIN scenes sfrom ON sfrom.id=fs.scene_id JOIN scenes sto ON sto.id=ts.scene_id "
            "WHERE c.active=1 AND c.scene_id IN (SELECT id FROM scenes WHERE production_id=?) ORDER BY c.id",
            (production_id,),
        ).fetchall()]

    def _day_payload(self, shoot_day_id: int, issues: list[dict] | None = None) -> dict:
        day = self.conn.execute("SELECT * FROM shoot_days WHERE id=?", (shoot_day_id,)).fetchone()
        if not day:
            raise DomainError("拍摄日不存在")
        entries = [dict(r) for r in self.conn.execute(
            "SELECT sds.scene_id,sds.queue_order,sc.scene_number,sc.title,sc.narrative_order "
            "FROM shoot_day_scenes sds JOIN scenes sc ON sc.id=sds.scene_id "
            "WHERE sds.shoot_day_id=? ORDER BY sds.queue_order", (shoot_day_id,)
        ).fetchall()]
        if issues is None:
            issues = self._production_issues(day["production_id"])
        blockers = precheck.blockers_for_day(issues, [e["scene_id"] for e in entries])
        return {
            **dict(day), "scenes": entries, "blockers": blockers,
            "releasable": bool(entries) and not blockers,
            "needs_recheck": day["status"] == "released" and bool(blockers),
        }

    def production_board(self, production_id: int) -> dict:
        """排期看板：待处理问题、拍摄日状态与阻断原因。"""
        production = self.conn.execute("SELECT * FROM productions WHERE id=?", (production_id,)).fetchone()
        if not production:
            raise DomainError("项目不存在")
        issues = self._production_issues(production_id)
        days = [
            self._day_payload(row["id"], issues)
            for row in self.conn.execute("SELECT id FROM shoot_days WHERE production_id=? ORDER BY id", (production_id,)).fetchall()
        ]
        scheduled: dict[int, list[dict]] = {}
        for r in self.conn.execute(
            "SELECT sds.scene_id,sds.shoot_day_id,sds.queue_order,sd.label "
            "FROM shoot_day_scenes sds JOIN shoot_days sd ON sd.id=sds.shoot_day_id WHERE sd.production_id=?",
            (production_id,),
        ).fetchall():
            scheduled.setdefault(r["scene_id"], []).append(
                {"day_id": r["shoot_day_id"], "label": r["label"], "queue_order": r["queue_order"]}
            )
        scenes = []
        for s in self.conn.execute("SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)).fetchall():
            scenes.append({
                "id": s["id"], "scene_number": s["scene_number"], "title": s["title"],
                "narrative_order": s["narrative_order"],
                "open_issues": len(precheck.blockers_for_scene(issues, s["id"])),
                "scheduled_in": scheduled.get(s["id"], []),
            })
        return {
            "production": dict(production),
            "pending_issues": [i for i in issues if precheck.is_blocking(i)],
            "exempted_issues": [i for i in issues if not precheck.is_blocking(i)],
            "scenes": scenes,
            "shoot_days": days,
        }

    def lock_shot(self, shot_id: int, user_id: int) -> None:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        if not shot:
            raise DomainError("镜头不存在")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权锁定镜头")
        with self.transaction():
            self._sync_conflicts(shot["scene_id"])
            blocking = self.conn.execute(
                "SELECT COUNT(*) FROM conflicts WHERE scene_id=? AND active=1 AND status!='exempted'", (shot["scene_id"],)
            ).fetchone()[0]
            if blocking:
                raise DomainError(f"场次仍有 {blocking} 个未处理冲突，不能锁定")
            self.conn.execute("UPDATE shots SET status='locked',version=version+1,updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), shot_id))

    def continuity_report(self, production_id: int) -> dict:
        production = self.conn.execute("SELECT * FROM productions WHERE id=?", (production_id,)).fetchone()
        if not production:
            raise DomainError("项目不存在")
        scenes = []
        for scene in self.conn.execute("SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)).fetchall():
            shots = [dict(r) for r in self.conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene["id"],))]
            conflicts = self.list_conflicts(scene["id"], include_resolved=True)
            scenes.append({**dict(scene), "shots": shots, "conflicts": conflicts})
        return {
            "production": dict(production),
            "elements": [dict(r) for r in self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (production_id,))],
            "scenes": scenes,
            "open_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "open"),
            "exempted_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "exempted"),
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "productions": [dict(r) for r in self.conn.execute("SELECT * FROM productions ORDER BY id")],
            "scenes": [dict(r) for r in self.conn.execute("SELECT * FROM scenes ORDER BY production_id,narrative_order")],
            "shots": [dict(r) for r in self.conn.execute("SELECT * FROM shots ORDER BY scene_id,narrative_order")],
            "shoot_days": [dict(r) for r in self.conn.execute("SELECT * FROM shoot_days ORDER BY id")],
            "shoot_day_scenes": [dict(r) for r in self.conn.execute("SELECT * FROM shoot_day_scenes ORDER BY shoot_day_id,queue_order")],
        }
