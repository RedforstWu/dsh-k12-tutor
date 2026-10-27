"""Local presentation preferences and task-scoped, registry-owned learning contexts."""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "themeId": "classic", "characterId": "none", "narrativeLevel": "light",
    "voice": {"enabled": False, "mode": "browser", "rate": 1, "voiceURI": "", "autoRead": False},
    "reducedMotion": True, "largeText": False, "interests": [],
}
INTERESTS = ("building", "exploration")


class ExperienceError(ValueError):
    pass


def themes() -> dict:
    return json.loads((ROOT / "themes" / "registry.json").read_text(encoding="utf-8"))


def characters() -> dict:
    return json.loads((ROOT / "characters" / "registry.json").read_text(encoding="utf-8"))


def theme(theme_id: str) -> dict:
    registry = themes()
    if not isinstance(theme_id, str) or theme_id not in registry:
        raise ExperienceError("themeId 必须是已注册的主题")
    return registry[theme_id]


def validate_preferences(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ExperienceError("体验偏好含有未支持的字段")
    clean = {**DEFAULTS, **value}
    theme(clean["themeId"])
    if not isinstance(clean["characterId"], str) or clean["characterId"] not in characters():
        raise ExperienceError("characterId 必须是已注册的角色")
    if clean["narrativeLevel"] not in ("off", "light"):
        raise ExperienceError("narrativeLevel 只能是 off 或 light")
    for key in ("reducedMotion", "largeText"):
        if type(clean[key]) is not bool:
            raise ExperienceError("动画和字体偏好必须是布尔值")
    voice = clean["voice"]
    if not isinstance(voice, dict) or set(voice) - set(DEFAULTS["voice"]):
        raise ExperienceError("voice 含有未支持的字段")
    voice = {**DEFAULTS["voice"], **voice}
    if (not isinstance(voice["voiceURI"], str) or len(voice["voiceURI"]) > 512
            or any(ord(char) < 32 for char in voice["voiceURI"]) or type(voice["autoRead"]) is not bool):
        raise ExperienceError("voice 需要有效音色标识及自动朗读开关")
    rate = voice["rate"]
    if (type(voice["enabled"]) is not bool or voice["mode"] != "browser"
            or type(rate) not in (int, float) or not math.isfinite(rate) or not 0.5 <= rate <= 2):
        raise ExperienceError("voice 需要浏览器朗读模式及 0.5 到 2 的语速")
    interests = clean["interests"]
    if (not isinstance(interests, list) or len(interests) > len(INTERESTS)
            or any(not isinstance(i, str) or i not in INTERESTS for i in interests)
            or len(set(interests)) != len(interests)):
        raise ExperienceError("interests 只能包含不重复的 building、exploration")
    return {**clean, "voice": voice, "interests": list(interests)}


def read_preferences(cwd: str | Path) -> dict:
    path = Path(cwd) / ".k12" / "experience.json"
    try:
        return validate_preferences(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return validate_preferences({})


def save_preferences(cwd: str | Path, value: dict) -> dict:
    clean = validate_preferences(value)
    path = Path(cwd) / ".k12" / "experience.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
        tmp = Path(stream.name)
        stream.write(json.dumps(clean, ensure_ascii=False, indent=2).encode("utf-8"))
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return clean


def task_context(preferences: dict, knowledge_points: list[str], answer_policy: str) -> dict:
    if answer_policy == "refuse" or preferences["narrativeLevel"] == "off":
        return {"themeId": "classic"}
    spec = theme(preferences["themeId"])
    for kp in knowledge_points:
        mapping = spec["contexts"].get(kp.split(":")[-1])
        if mapping:
            return {"themeId": preferences["themeId"], "context": mapping,
                    "instruction": "原创教学模拟。先预测、操作观察、写等量关系，再验算并迁移到无主题题；保持当前答案策略。"}
    return {"themeId": "classic"}


def presentation_css(theme_id: str, preferences: dict | None = None) -> str:
    spec = theme(theme_id)
    tokens = ";".join(f"--{key}:{value}" for key, value in spec["tokens"].items())
    css = (f":root{{{tokens}}}body{{background:var(--background);color:var(--ink)}}"
           ".panel,svg{background:var(--surface);border-color:var(--border)}"
           "button{background:var(--button);border-color:var(--border);color:var(--ink)}"
           "button.primary{background:var(--accent);border-color:var(--accent);color:#fff}"
           ".lead{color:var(--muted)}.back{background:var(--button);color:var(--ink)}"
           "input,select,option{background:var(--surface);color:var(--ink)}")
    if preferences and preferences["largeText"]:
        css += "body{font-size:19px}h1{font-size:23px}.math{font-size:25px}"
    if not preferences or preferences["reducedMotion"]:
        css += "*,*::before,*::after{animation:none!important;transition:none!important}"
    return css
