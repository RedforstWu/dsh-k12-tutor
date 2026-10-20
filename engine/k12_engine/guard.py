"""Deterministic safety checks around each tutor turn.

``inspect_input`` runs on the student's message before the model sees it and returns notes the plugin
injects into that step. ``inspect_reply`` runs on the tutor's finished reply and returns violations the
plugin sends back as steering. Both are pattern tables, so they cost microseconds and never skip a turn.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern
    note: str


def _re(*alternatives: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{a})" for a in alternatives))


CRISIS_NOTE = ("[安全规则 crisis] 学生的话里可能有自伤、轻生或正在受到伤害的信号。这一轮先放下学习任务："
               "认真接住学生的感受，直接而温和地问清是否安全；明确建议马上告诉身边信任的大人（家长、班主任或心理老师）；"
               "给出求助渠道：全国心理援助热线 12356、青少年服务台 12355，有紧急危险打 110 或 120。"
               "不评判，不讲道理，不承诺保密，不继续讲题。系统已为家长或老师记录了一条提醒。")
INPUT_RULES = (
    Rule("crisis", _re(r"自杀", r"轻生", r"不想活", r"活着(?:没|没有)(?:意思|意义)", r"想死", r"去死", r"死了算了",
                       r"结束(?:自己的)?生命", r"割腕", r"跳楼", r"伤害自己", r"自残", r"遗书", r"活不下去",
                       r"消失(?:就好|算了)", r"没人在乎我"), CRISIS_NOTE),
    Rule("harm", _re(r"(?:被|挨)(?:打|揍|欺负|霸凌|威胁|勒索)", r"家暴", r"性侵", r"猥亵", r"有人(?:摸|碰)我",
                     r"不让我告诉(?:别人|爸妈|老师)"),
         "[安全规则 harm] 学生可能正在受到欺凌、暴力或侵害。先关心安全，鼓励马上告诉家长、班主任或学校，"
         "必要时拨打 110；可以提到青少年服务台 12355。不追问细节，不让学生独自处理。系统已为家长或老师记录了一条提醒。"),
    Rule("exam-live", _re(r"正在考试", r"考试中", r"在考场", r"考场上", r"监考", r"(?:现在|正在)(?:在)?(?:测验|月考|期中考|期末考|考试)",
                          r"考完(?:之前|前)", r"偷偷(?:用|拿)手机"),
         "[诚信规则 exam-live] 学生可能正在考试。不讲这道题，不给提示，温和说明考后可以一起复盘，"
         "route_task 的 assessment_context 记为 exam-live。"),
    Rule("ghostwrite", _re(r"代写", r"帮我写.{0,8}(?:作文|周记|日记|读后感|检讨|作业)", r"替我(?:写|做)",
                           r"帮我(?:把)?作业(?:做|写)(?:完|了)", r"抄(?:一下|给我)"),
         "[诚信规则 ghostwrite] 学生想让你替他完成作业或作文。不代写；把请求转成帮他自己完成"
         "（列提纲、给思路、评改他写的句子），先问他已经想到了什么。"),
    Rule("answer-demand", _re(r"直接(?:告诉我|给我|说)(?:答案|结果)", r"只要答案", r"(?:给我|说出)答案", r"答案是(?:多少|什么)",
                              r"别(?:讲|问)了.{0,6}答案", r"不想(?:学|听).{0,6}答案"),
         "[诚信规则 answer-demand] 学生在要答案。先承认他的着急，说明你会陪他很快做出来，"
         "请他先说出第一步或卡住的地方；按逐级提示推进，不直接给最终答案。"),
    Rule("pii", _re(r"(?<!\d)1[3-9]\d{9}(?!\d)", r"(?<!\d)\d{17}[\dXx](?!\d)", r"我(?:家)?住在", r"我家地址", r"我的(?:微信|QQ|qq)号"),
         "[隐私规则 pii] 学生说出了可能识别身份的信息。不要复述，不要记进学情档案；简短提醒他网上不要透露这些。"),
)

CUE = r"(?:答案|结果|解|得数|等于|是|为|得|所以|∴|=|＝)"
# A bare value that is also the value of an "x=..." solution counts only after an explicit answer word: in a
# substitution check such as 3x-2=9-2=7 the number can coincide with the answer.
STRONG_CUE = r"(?:答案是|答案为|结果是|结果为|解是|解为|解得|得数是|所以x=|答案：|答案:)"
REWRITE = "学生只会看到你这一轮的最后一条消息，所以请把这一轮给学生的回复完整重写一遍："
ADULT = re.compile(r"家长|爸爸|妈妈|父母|老师|班主任|心理|大人")
HOTLINE = re.compile(r"12356|12355")
CRISIS_TEMPLATE = ("如果你现在有伤害自己的想法，或者正在受到伤害，请马上告诉身边信任的大人（爸爸妈妈、班主任或心理老师）。"
                   "也可以打电话：全国心理援助热线 12356，青少年服务台 12355。有紧急危险请打 110 或 120。")
PERSONA_CLAIM = re.compile(r"我是真人|我不是AI|我不是机器人|我是你的(?:女|男)朋友|做你的?(?:女|男)朋友|我也(?:爱|喜欢)你|我们是(?:恋人|情侣)")
PII_REQUEST = re.compile(r"(?:告诉我|发我|留下|说一下|提供)(?:你的)?(?:手机号|电话|住址|家庭地址|身份证|真实姓名|真名|学校名|微信|QQ)")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s\$\\{}*]+", "", text).replace("−", "-").replace("﹣", "-")


def inspect_input(text: str) -> list[dict]:
    flags = []
    for rule in INPUT_RULES:
        match = rule.pattern.search(text or "")
        if match:
            flags.append({"kind": rule.kind, "match": match.group(0), "note": rule.note})
    return flags


def _answer_patterns(answer: str) -> list[re.Pattern]:
    """Patterns that mean the reply states this verified answer, not merely mentions the number."""
    a = normalize(answer)
    if not a or a in ("无解",):
        return [re.compile(re.escape(a))] if a else []
    if "=" in a:
        var, _, value = a.partition("=")
        return [re.compile(rf"(?<![A-Za-z0-9.]){re.escape(var)}={re.escape(value)}(?![0-9.A-Za-z(])")]
    if re.fullmatch(r"-?\d+(?:\.\d+)?|-?\d+/\d+|√\(?\d+\)?", a):
        return [re.compile(rf"{CUE}{re.escape(a)}(?![0-9./A-Za-z(√^²])")]
    return [re.compile(re.escape(a))]


def _student_patterns(answer: str) -> list[re.Pattern]:
    """The student counts as having said it in either form: "x=7", or "...=7" / "是7" at the end of their own work."""
    a = normalize(answer)
    patterns = _answer_patterns(answer)
    if "=" in a:
        patterns += _answer_patterns(a.partition("=")[2])
    return patterns


def stated_answers(reply: str, answers: list[str]) -> list[str]:
    text = normalize(reply)
    found = []
    for a in answers:
        value = normalize(a)
        if "=" not in value and any(normalize(b).endswith("=" + value) for b in answers):
            patterns = [re.compile(rf"{STRONG_CUE}{re.escape(value)}(?![0-9./A-Za-z(√^²])")]
        else:
            patterns = _answer_patterns(a)
        if any(p.search(text) for p in patterns):
            found.append(a)
    return found


def inspect_reply(reply: str, state: dict) -> list[dict]:
    """``state``: answer_policy, verified answers of the open task, the student's texts in that task,
    and the input flags of this turn."""
    violations = []
    flags = {f["kind"] for f in state.get("flags", [])}
    if flags & {"crisis", "harm"} and not (ADULT.search(reply) and HOTLINE.search(reply)):
        violations.append({"kind": "crisis-unreferred",
                           "steer": "[安全规则] 上一条回复没有同时把学生引向身边的大人和求助热线。" + REWRITE + "先接住他的感受、关心他现在是否安全，"
                                    f"然后原样附上这段话：{CRISIS_TEMPLATE}"})
    if PERSONA_CLAIM.search(reply):
        violations.append({"kind": "persona-claim",
                           "steer": "[诚实规则] 上一条回复把自己说成了真人或学生的朋友、恋人。" + REWRITE + "说明你是 AI 学习导师，"
                                    "不能做朋友或恋人，然后把话题带回学习。"})
    if PII_REQUEST.search(reply):
        violations.append({"kind": "pii-request",
                           "steer": "[隐私规则] 上一条回复向学生索要了可识别身份的信息。" + REWRITE + "保留有用的内容，说明不需要这些信息，也提醒他不要在网上透露。"})
    policy = state.get("answer_policy")
    if policy in ("withhold", "refuse"):
        said_by_student = normalize(" ".join(state.get("student_texts", [])))
        leaked = [a for a in stated_answers(reply, state.get("answers", []))
                  if not any(p.search(said_by_student) for p in _student_patterns(a))]
        if leaked:
            violations.append({"kind": "answer-leak", "answers": leaked,
                               "steer": f"[答案规则] 上一条回复把这道题的最终答案（{'，'.join(leaked)}）直接告诉了学生，"
                                        "而学生还没有自己算出来。" + REWRITE + "保留上一条里有用的讲解和提问，去掉最终答案，"
                                        "不要提这条提醒，用「你」称呼学生，最后请他自己算出这一步。"})
    return violations
