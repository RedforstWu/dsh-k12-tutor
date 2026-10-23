"""Subject packs: subject / stage / textbook edition / knowledge point, loaded from ``subjects/``.

A pack is edition-neutral at the knowledge-point level: ``kp-index.yaml`` lists every knowledge point
(name, grade, domain, aliases, prerequisites, misconceptions); ``kp/<id>.md`` adds curriculum references
and the teaching body (explanation, diagnostic questions, worked examples, variants) for the points that
have full content. Each textbook edition maps its chapters onto those ids (``textbooks/<edition>.yaml``).
Other subjects and stages are listed in ``subjects/index.yaml`` with ``status: planned`` and no content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SUBJECTS = ROOT / "subjects"
KP_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


class PackError(ValueError):
    pass


@dataclass(frozen=True)
class Misconception:
    id: str
    name: str
    symptom: str
    diagnostic: str
    remedy: str
    sources: tuple[str, ...]
    level: int  # evidence level 1-5; 5 is an unverified hypothesis, used only as an observation point


@dataclass(frozen=True)
class KnowledgePoint:
    id: str
    name: str
    grade: int
    domain: str
    aliases: tuple[str, ...]
    curriculum: tuple[str, ...]
    prerequisites: tuple[str, ...]
    misconceptions: tuple[str, ...]
    status: str  # full | outline
    body: str
    path: Path | None  # the kp/<id>.md file; None for an outline point
    examples: tuple[dict, ...] = ()  # worked examples: problem, check (a math_check call), expect, answer, errors, hints
    explanation_summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    no: str
    title: str
    kps: tuple[str, ...]


@dataclass(frozen=True)
class Chapter:
    grade: int
    term: str
    no: int
    title: str
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class Pack:
    subject: str
    stage: str
    name: str
    grades: tuple[int, ...]
    curriculum: dict
    kps: dict[str, KnowledgePoint]
    misconceptions: dict[str, Misconception]
    textbooks: dict[str, tuple[Chapter, ...]] = field(default_factory=dict)
    textbook_meta: dict[str, dict] = field(default_factory=dict)
    unplaced: frozenset[str] = frozenset()

    @property
    def key(self) -> str:
        return f"{self.subject}/{self.stage}"

    def chapters_for(self, kp_id: str, edition: str) -> list[tuple[Chapter, Section]]:
        return [(c, s) for c in self.textbooks.get(edition, ()) for s in c.sections if kp_id in s.kps]

    def locate(self, kp_id: str, edition: str | None) -> tuple[str | None, list[tuple[Chapter, Section]]]:
        """Where the point sits in the student's edition, else in the first edition that has it."""
        for name in ([edition] if edition in self.textbooks else []) + [e for e in self.textbooks if e != edition]:
            found = self.chapters_for(kp_id, name)
            if found:
                return name, found
        return None, []


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PackError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError(f"{path}: expected a mapping")
    return data


def _tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _kp(kp_id: str, meta: dict, body: str, path: Path | None) -> KnowledgePoint:
    if not KP_ID.match(kp_id):
        raise PackError(f"knowledge point id {kp_id!r} must be kebab-case")
    for key in ("name", "grade", "domain"):
        if not meta.get(key):
            raise PackError(f"knowledge point {kp_id}: missing {key}")
    status = "full" if path else "outline"
    summary = meta.get("explanation_summary", [])
    if (not isinstance(summary, list) or len(summary) > 2
            or any(not isinstance(line, str) or not line.strip() for line in summary)):
        raise PackError(f"knowledge point {kp_id}: explanation_summary must be a list of at most two nonempty strings")
    return KnowledgePoint(
        id=kp_id, name=str(meta["name"]), grade=int(meta["grade"]), domain=str(meta["domain"]),
        aliases=_tuple(meta.get("aliases")), curriculum=_tuple(meta.get("curriculum")),
        prerequisites=_tuple(meta.get("prerequisites")), misconceptions=_tuple(meta.get("misconceptions")),
        status=status, body=body, path=path, examples=tuple(meta.get("examples") or ()),
        explanation_summary=tuple(line.strip() for line in summary))


def load_kps(directory: Path) -> dict[str, KnowledgePoint]:
    """kp-index.yaml entries, each merged with its kp/<id>.md when one exists (the file wins per field)."""
    index_entries = _load_yaml(directory / "kp-index.yaml").get("kps") or {}
    kps = {}
    files = {p.stem: p for p in sorted((directory / "kp").glob("*.md"))}
    for kp_id in files.keys() - index_entries.keys():
        raise PackError(f"kp/{kp_id}.md has no entry in kp-index.yaml")
    for kp_id, entry in index_entries.items():
        meta, body, path = dict(entry or {}), "", None
        if kp_id in files:
            path = files[kp_id]
            match = FRONTMATTER.match(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
            if not match:
                raise PackError(f"{path}: missing YAML frontmatter")
            extra = yaml.safe_load(match.group(1)) or {}
            if extra.get("id") != kp_id:
                raise PackError(f"{path}: frontmatter id must equal the file name")
            meta.update({k: v for k, v in extra.items() if k != "id"})
            body = match.group(2).strip()
        kps[kp_id] = _kp(kp_id, meta, body, path)
    return kps


def load_pack(directory: Path) -> Pack:
    meta = _load_yaml(directory / "pack.yaml")
    if meta.get("schema") != "k12-subject-pack/1":
        raise PackError(f"{directory}: pack.yaml schema must be k12-subject-pack/1")
    kps = load_kps(directory)
    raw_mc = _load_yaml(directory / "misconceptions.yaml").get("misconceptions") or {}
    misconceptions = {}
    for mc_id, entry in raw_mc.items():
        if not KP_ID.match(mc_id):
            raise PackError(f"misconception id {mc_id!r} must be kebab-case")
        misconceptions[mc_id] = Misconception(
            id=mc_id, name=str(entry["name"]), symptom=str(entry["symptom"]), diagnostic=str(entry["diagnostic"]),
            remedy=str(entry["remedy"]), sources=_tuple(entry.get("sources")), level=int(entry.get("level", 5)))
    textbooks, textbook_meta = {}, {}
    for edition, rel in (meta.get("textbooks") or {}).items():
        book = _load_yaml(directory / rel)
        textbook_meta[edition] = {k: v for k, v in book.items() if k != "volumes"}
        chapters = []
        for volume in book.get("volumes") or []:
            for chapter in volume.get("chapters") or []:
                sections = tuple(Section(str(s["n"]), str(s["title"]), _tuple(s.get("kps")))
                                 for s in chapter.get("sections") or [])
                chapters.append(Chapter(int(volume["grade"]), str(volume["term"]), int(chapter["n"]),
                                        str(chapter["title"]), sections))
        textbooks[edition] = tuple(chapters)
    unplaced = frozenset(k for k, e in (_load_yaml(directory / "kp-index.yaml").get("kps") or {}).items()
                         if (e or {}).get("unplaced"))
    return Pack(subject=str(meta["subject"]), stage=str(meta["stage"]), name=str(meta.get("name", "")),
                grades=tuple(int(g) for g in meta.get("grades") or ()), curriculum=meta.get("curriculum") or {},
                kps=kps, misconceptions=misconceptions, textbooks=textbooks, textbook_meta=textbook_meta,
                unplaced=unplaced)


@lru_cache(maxsize=None)
def index(root: Path = SUBJECTS) -> dict:
    data = _load_yaml(root / "index.yaml")
    if data.get("schema") != "k12-subject-index/1":
        raise PackError("subjects/index.yaml schema must be k12-subject-index/1")
    return data


@lru_cache(maxsize=None)
def pack(subject: str, stage: str, root: Path = SUBJECTS) -> Pack:
    for entry in index(root)["subjects"]:
        if entry["id"] != subject:
            continue
        for st in entry.get("stages") or []:
            if st["id"] == stage:
                if st.get("status") != "active":
                    raise PackError(f"{subject}/{stage} is {st.get('status', 'planned')}: no content yet")
                return load_pack(root / st["pack"])
    raise PackError(f"no subject pack {subject}/{stage}")


def stage_of(grade: int | None) -> str | None:
    if grade is None:
        return None
    if 1 <= grade <= 6:
        return "primary"
    if 7 <= grade <= 9:
        return "junior"
    if 10 <= grade <= 12:
        return "senior"
    return None


def validate(p: Pack) -> list[str]:
    """Cross-reference problems: unknown prerequisite, misconception or catalog ids."""
    problems = []
    for kp in p.kps.values():
        problems += [f"{kp.id}: unknown prerequisite {x}" for x in kp.prerequisites if x not in p.kps]
        problems += [f"{kp.id}: unknown misconception {x}" for x in kp.misconceptions if x not in p.misconceptions]
        if kp.grade not in p.grades:
            problems.append(f"{kp.id}: grade {kp.grade} outside pack grades")
    for edition, chapters in p.textbooks.items():
        for c in chapters:
            for s in c.sections:
                problems += [f"{edition} {s.no}: unknown kp {x}" for x in s.kps if x not in p.kps]
    placed = {k for chapters in p.textbooks.values() for c in chapters for s in c.sections for k in s.kps}
    problems += [f"{k}: in no textbook catalog (add `unplaced: true` in kp-index.yaml if intended)"
                 for k, kp in p.kps.items() if k not in placed and k not in p.unplaced]
    for kp in p.kps.values():
        for ex in kp.examples:
            problems += [f"{kp.id} example {ex.get('id')}: missing {k}" for k in ("id", "problem", "check", "expect", "answer")
                         if k not in ex]
            problems += [f"{kp.id} example {ex.get('id')}: unknown misconception {m}" for m in ex.get("errors") or []
                         if m not in kp.misconceptions]
    used = {m for kp in p.kps.values() for m in kp.misconceptions}
    problems += [f"misconception {m} is used by no knowledge point" for m in p.misconceptions if m not in used]
    return problems
