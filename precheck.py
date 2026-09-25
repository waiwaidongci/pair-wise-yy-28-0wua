"""整片预检与拍摄日放行的检查逻辑。

只基于取出的数据做判定，不直接写库：数据存取在 database.py，
页面与接口操作在 app.py / static/index.html。
"""
from __future__ import annotations


def evaluate_transition(element, prev, curr, allowed) -> tuple[str | None, str]:
    """按元素规则判定一次状态转移，返回 (kind, detail)，无冲突时 kind 为 None。

    element/prev/curr 为含 rule/state_value/numeric_value 的行，
    allowed 为 allowed 规则元素已登记的 (from_state, to_state) 集合。
    场内与跨场比对共用这一套规则。
    """
    rule = element["rule"]
    if rule == "stable":
        if curr["state_value"] != prev["state_value"]:
            return "state_changed", f"{element['name']} 应为稳定状态，却从 {prev['state_value']} 变为 {curr['state_value']}"
    elif rule == "monotonic":
        if curr["numeric_value"] is None or prev["numeric_value"] is None:
            return "missing_numeric_value", f"{element['name']} 缺少可比较的数值"
        if curr["numeric_value"] < prev["numeric_value"]:
            return "regression", f"{element['name']} 在叙事顺序中从 {prev['numeric_value']} 回退到 {curr['numeric_value']}"
    else:
        if (prev["state_value"], curr["state_value"]) not in allowed:
            return "transition_not_allowed", f"{element['name']} 不允许从 {prev['state_value']} 变为 {curr['state_value']}"
    return None, ""


def detect_boundary_conflicts(elements, transitions_by_element, scenes, shots_by_scene, states_by_shot) -> list[dict]:
    """按叙事顺序比对相邻场次的首尾状态，返回跨场冲突（未写库）。

    对每个元素沿“场次叙事顺序 + 场次内镜头叙事顺序”扫描有状态的镜头，
    相邻两条状态记录跨越场次时按元素规则判定；没有登记状态的场次自然跳过。
    """
    detected: list[dict] = []
    for element in elements:
        allowed = transitions_by_element.get(element["id"], set())
        prev = None  # (scene, shot, state)，上一场最后一个有状态的记录
        for scene in scenes:
            for shot in shots_by_scene.get(scene["id"], []):
                state = states_by_shot.get((shot["id"], element["id"]))
                if state is None:
                    continue
                if prev is not None and prev[0]["id"] != scene["id"]:
                    kind, detail = evaluate_transition(element, prev[2], state, allowed)
                    if kind:
                        detected.append({
                            "scope": "cross",
                            "from_scene_id": prev[0]["id"],
                            "scene_id": scene["id"],
                            "element_id": element["id"],
                            "element_name": element["name"],
                            "from_shot_id": prev[1]["id"],
                            "to_shot_id": shot["id"],
                            "kind": kind,
                            "detail": detail,
                            "fingerprint": f"cross:{prev[0]['id']}:{scene['id']}:{element['id']}:{prev[1]['id']}:{shot['id']}:{kind}",
                        })
                prev = (scene, shot, state)
    return detected


def is_blocking(issue) -> bool:
    """未处理 = 仍然活跃且未豁免。"""
    return bool(issue["active"]) and issue["status"] != "exempted"


def blockers_for_scene(issues, scene_id) -> list:
    """场次维度的阻断项：场内冲突，或跨场冲突中作为前/后场的。"""
    return [i for i in issues if is_blocking(i) and scene_id in (i["scene_id"], i["from_scene_id"])]


def blockers_for_day(issues, scene_ids) -> list:
    """拍摄日维度的阻断项：涉及任一已排期场次的未处理问题。"""
    ids = set(scene_ids)
    return [i for i in issues if is_blocking(i) and (i["scene_id"] in ids or i["from_scene_id"] in ids)]


def blocker_summary(blockers) -> str:
    """把阻断项整理成可展示的原因串。"""
    details: list[str] = []
    for issue in blockers:
        if issue["detail"] not in details:
            details.append(issue["detail"])
    return "；".join(details)
