"""Deterministic prompts over explicit, editable Chinese creative-note labels."""

import re


def _note_value(notes, label):
    match = re.search(rf"(?m)^[ \t]*{re.escape(label)}[ \t]*[:：][ \t]*(.*)$", notes)
    return match.group(1).strip() if match else ""


def review_story(episode: dict) -> list[dict]:
    findings = []

    def add(scene_id, shot_id, field, suggestion):
        findings.append({"episode_id": episode.get("id"), "scene_id": scene_id, "shot_id": shot_id, "field": field, "suggestion": suggestion, "confidence": "structural", "limitation": "仅依据已填写文本；未观看画面或判断观众反应"})

    scenes = episode.get("scenes", [])
    notes = episode.get("creative_notes", "")
    previous = None
    for scene in scenes:
        scene_id = scene.get("id")
        scene_time = scene.get("time_of_day") or _note_value(notes, f"场景时间[{scene_id}]")
        if not scene.get("purpose", "").strip():
            add(scene_id, None, "purpose", "写明这场希望人物达成的目标")
        if not scene.get("location", "").strip():
            add(scene_id, None, "location", "标出场景空间，便于检查转场")
        if previous and (previous.get("location") != scene.get("location") or previous_time != scene_time) and not scene.get("transition", "").strip():
            add(scene_id, None, "transition", "空间或时间变化需要可见或可听的转场线索")
        for shot in scene.get("shots", []):
            shot_id = shot.get("id")
            for field, message in (("story_job", "写明镜头的叙事任务或人物选择"), ("start_state", "写明动作前状态"), ("action", "写明可见行动及动机"), ("end_state", "写明行动造成的结果"), ("transition", "写明通往下一镜的线索")):
                if not shot.get(field, "").strip():
                    add(scene_id, shot_id, field, message)
            missing = []
            for label, key in (("动机", "motivation"), ("选择", "choice")):
                explicit = bool(str(shot.get(key, "")).strip()) or bool(re.search(rf"(?m)^[ \t]*{label}\[{re.escape(str(shot_id))}\][ \t]*[:：][ \t]*[^\s]", notes))
                if not explicit:
                    missing.append(label)
            if missing:
                add(scene_id, shot_id, "creative_notes", f"在分集创作备注中按“{missing[0]}[{shot_id}]: 内容”格式补充{'与'.join(missing)}；只检查明示记录，不能推断隐含意图")
        previous = scene
        previous_time = scene_time
    if scenes and not (episode.get("next_expectation") or _note_value(notes, "续集期待")).strip():
        add(scenes[-1].get("id"), None, "next_expectation", "说明结尾希望留下的具体悬念或期待")
    return findings
