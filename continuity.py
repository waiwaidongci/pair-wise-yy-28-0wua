# -*- coding: utf-8 -*-
"""连续性检测规则（纯逻辑层）。

本模块只承载检查逻辑，不依赖数据库（database.py）和页面/HTTP 操作（app.py）：
传入场次、镜头、元素状态和已登记的状态转移，输出按叙事顺序发现的连续性问题。

问题分两种范围：
- within：同一场次内相邻镜头之间；
- cross：叙事顺序相邻的两个场次之间，取前场最后一个带该元素状态的镜头
  （尾）与后场第一个带状态的镜头（首）比对。
两种范围沿用同一套元素规则（stable / monotonic / allowed）。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

STABLE = "stable"
MONOTONIC = "monotonic"
ALLOWED = "allowed"
RULES = {STABLE, MONOTONIC, ALLOWED}

SCOPES = {"within", "cross"}


def compare_states(element: dict, prev_state: dict, current_state: dict,
                   transitions: set[tuple[int, str, str]]) -> tuple[str, str] | None:
    """按元素配置的规则比较相邻两个状态，返回 (kind, detail) 或 None。"""
    name = element["name"]
    rule = element["rule"]
    prev_value = prev_state["state_value"]
    current_value = current_state["state_value"]
    if rule == STABLE:
        if prev_value != current_value:
            return "state_changed", f"{name} 应为稳定状态，却从 {prev_value} 变为 {current_value}"
    if rule == MONOTONIC:
        prev_num = prev_state.get("numeric_value")
        current_num = current_state.get("numeric_value")
        if prev_num is None or current_num is None:
            return "missing_numeric_value", f"{name} 缺少可比较的数值"
        if current_num < prev_num:
            return "regression", f"{name} 在叙事顺序中从 {prev_num} 回退到 {current_num}"
        return None
    # allowed：只有预先登记的状态转移才能通过
    if (element["id"], prev_value, current_value) not in transitions:
        return "transition_not_allowed", f"{name} 不允许从 {prev_value} 变为 {current_value}"
    return None


def _make_issue(element: dict, scope: str, transitions: set[tuple[int, str, str]],
                prev_shot: dict, prev_state: dict, shot: dict,
                current_state: dict, to_scene_id: int) -> dict | None:
    found = compare_states(element, prev_state, current_state, transitions)
    if not found:
        return None
    kind, detail = found
    if scope == "cross":
        detail = "跨场衔接：" + detail
    return {
        "element_id": element["id"],
        "from_shot_id": prev_shot["id"],
        "to_shot_id": shot["id"],
        "scene_id": to_scene_id,  # 跨场问题挂在叙事下游场次，由它承担排期阻断
        "scope": scope,
        "kind": kind,
        "detail": detail,
        "fingerprint": f"{element['id']}:{prev_shot['id']}:{shot['id']}:{kind}",
    }


def find_issues(scenes: Sequence[dict], elements: Iterable[dict], shots: Sequence[dict],
                states: dict[tuple[int, int], dict],
                transitions: set[tuple[int, str, str]]) -> list[dict]:
    """按叙事顺序检测单场内冲突与相邻场次首尾的跨场冲突。

    scenes 必须按 narrative_order 排好序；shots 含 scene_id 与镜头叙事顺序；
    states 以 (shot_id, element_id) 为键。
    """
    shots_by_scene: dict[int, list[dict]] = {}
    for shot in shots:
        shots_by_scene.setdefault(shot["scene_id"], []).append(shot)

    issues: list[dict] = []
    for element in elements:
        scene_sequences: list[tuple[dict, list[tuple[dict, dict]]]] = []
        for scene in scenes:
            ordered = sorted(shots_by_scene.get(scene["id"], []),
                             key=lambda s: s["narrative_order"])
            sequence: list[tuple[dict, dict]] = []
            for shot in ordered:
                state = states.get((shot["id"], element["id"]))
                if state is not None:
                    sequence.append((shot, state))
            scene_sequences.append((scene, sequence))
            # 单场内：沿镜头叙事顺序逐对比对
            for (prev_shot, prev_state), (shot, current_state) in zip(sequence, sequence[1:]):
                issue = _make_issue(element, "within", transitions,
                                    prev_shot, prev_state, shot, current_state, scene["id"])
                if issue:
                    issues.append(issue)
        # 跨场：相邻场次都有该元素的状态时，比对前场之尾与后场之首
        for (_, prev_sequence), (next_scene, next_sequence) in zip(scene_sequences, scene_sequences[1:]):
            if prev_sequence and next_sequence:
                prev_shot, prev_state = prev_sequence[-1]
                shot, current_state = next_sequence[0]
                issue = _make_issue(element, "cross", transitions,
                                    prev_shot, prev_state, shot, current_state, next_scene["id"])
                if issue:
                    issues.append(issue)
    return issues
