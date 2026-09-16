"""Детерминированный скоринг без LLM ([CORE-015]).

Ни одного сетевого вызова: стек, вилка, гео и стоп-слова считаются локально.
Именно поэтому MVP работает до того, как появится шлюз и Ollama.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz

FUZZY_THRESHOLD = 88


@dataclass
class Profile:
    queries: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    stop_words: list[str] = field(default_factory=list)
    min_salary_net: int = 0
    allow_missing_salary: bool = True
    areas: list[int] = field(default_factory=list)
    remote_ok: bool = True
    experience_ok: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=dict)
    min_score: float = 45.0
    facts: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        salary = raw.get("salary") or {}
        geo = raw.get("geo") or {}
        return cls(
            queries=raw.get("queries") or [],
            skills=[s.lower() for s in raw.get("skills") or []],
            nice_to_have=[s.lower() for s in raw.get("nice_to_have") or []],
            stop_words=[s.lower() for s in raw.get("stop_words") or []],
            min_salary_net=int(salary.get("min_net") or 0),
            allow_missing_salary=bool(salary.get("allow_missing", True)),
            areas=[int(a) for a in geo.get("areas") or []],
            remote_ok=bool(geo.get("remote_ok", True)),
            experience_ok=raw.get("experience_ok") or [],
            weights=raw.get("weights") or {},
            min_score=float(raw.get("min_score", 45)),
            facts=raw.get("facts") or [],
        )

    def weight(self, name: str, default: int) -> int:
        return int(self.weights.get(name, default))


@dataclass
class Verdict:
    score: float
    reasons: list[str]
    rejected: bool = False
    reject_reason: str | None = None


def _haystack(vacancy: Any) -> str:
    parts = [vacancy.title, vacancy.description, " ".join(vacancy.skills)]
    return " ".join(p for p in parts if p).lower()


def _matches(term: str, haystack: str) -> bool:
    if term in haystack:
        return True
    return fuzz.partial_ratio(term, haystack) >= FUZZY_THRESHOLD


def evaluate(vacancy: Any, profile: Profile) -> Verdict:
    haystack = _haystack(vacancy)
    reasons: list[str] = []

    # 1. Стоп-слова — жёсткий отказ до любых баллов.
    for word in profile.stop_words:
        if word and word in haystack:
            return Verdict(0.0, [], rejected=True, reject_reason=f"стоп-слово: {word}")

    # 2. Зарплата. Отсутствие вилки — не отказ, а штраф: на hh.ru много хороших
    #    вакансий без указанной вилки, и их скрытие само по себе сигнал.
    salary_weight = profile.weight("salary", 20)
    salary_net = vacancy.monthly_salary_net()
    if salary_net is None:
        if not profile.allow_missing_salary:
            return Verdict(0.0, [], rejected=True, reject_reason="вилка не указана")
        salary_points = salary_weight * 0.4
        reasons.append("вилка не указана")
    elif profile.min_salary_net and salary_net < profile.min_salary_net * 0.85:
        return Verdict(
            0.0,
            [],
            rejected=True,
            reject_reason=f"вилка ниже порога: {salary_net}",
        )
    elif profile.min_salary_net and salary_net < profile.min_salary_net:
        salary_points = salary_weight * 0.6
        reasons.append(f"вилка чуть ниже цели: {salary_net:,} net".replace(",", " "))
    else:
        salary_points = float(salary_weight)
        if salary_net:
            reasons.append(f"вилка {salary_net:,} net".replace(",", " "))

    # 3. Навыки — основной вес.
    skills_weight = profile.weight("skills", 55)
    matched = [s for s in profile.skills if _matches(s, haystack)]
    if profile.skills:
        skills_points = skills_weight * len(matched) / len(profile.skills)
    else:
        skills_points = 0.0
    if matched:
        reasons.append("стек: " + ", ".join(matched[:6]))

    bonus_weight = profile.weight("nice_to_have", 10)
    bonus_matched = [s for s in profile.nice_to_have if _matches(s, haystack)]
    bonus_points = (
        bonus_weight * len(bonus_matched) / len(profile.nice_to_have)
        if profile.nice_to_have
        else 0.0
    )
    if bonus_matched:
        reasons.append("бонус: " + ", ".join(bonus_matched[:4]))

    # 4. Формат работы.
    remote_weight = profile.weight("remote", 10)
    is_remote = "удален" in (vacancy.schedule or "").lower() or "remote" in haystack
    remote_points = float(remote_weight) if (is_remote and profile.remote_ok) else 0.0
    if is_remote:
        reasons.append("удалёнка")

    # 5. Опыт.
    exp_weight = profile.weight("experience", 5)
    if profile.experience_ok and vacancy.experience:
        exp_points = float(exp_weight) if vacancy.experience in profile.experience_ok else 0.0
        if not exp_points:
            reasons.append(f"требования по опыту: {vacancy.experience}")
    else:
        exp_points = float(exp_weight) * 0.5

    total = salary_points + skills_points + bonus_points + remote_points + exp_points
    return Verdict(round(min(total, 100.0), 1), reasons)
