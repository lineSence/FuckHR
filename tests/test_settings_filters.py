"""Настройки предфильтра и детектора: значения, границы и влияние на прогон."""

from __future__ import annotations

import detector
import pytest
import run
import score
import settings


def test_значения_по_умолчанию(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "PREFILTER_ENABLED",
        "PREFILTER_MIN_SCORE",
        "PREFILTER_FUZZY",
        "DETECTOR_ENABLED",
        "DETECTOR_MIN_DAYS",
        "DETECTOR_REPUBLISH_ALARM",
        "DETECTOR_WIDE_BAND",
        "DETECTOR_LLM_CLAIMS",
        "LLM_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    assert settings.prefilter_options() == settings.PrefilterOptions(True, 0.0, 88)
    assert settings.detector_options() == settings.DetectorOptions(True, 30, 3, 2.0, True)


def test_пороги_не_уезжают_в_бессмыслицу(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нулевая история и вилка «шире чем в 0 раз» дали бы выводы из ничего."""
    monkeypatch.setenv("PREFILTER_FUZZY", "10")
    monkeypatch.setenv("DETECTOR_MIN_DAYS", "0")
    monkeypatch.setenv("DETECTOR_REPUBLISH_ALARM", "1")
    monkeypatch.setenv("DETECTOR_WIDE_BAND", "0")
    assert settings.prefilter_options().fuzzy == 50
    limits = settings.detector_options()
    assert (limits.min_days, limits.republish_alarm, limits.wide_band) == (1, 2, 1.1)


def test_подсказка_модели_гаснет_вместе_с_моделью(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DETECTOR_LLM_CLAIMS", "1")
    monkeypatch.setenv("LLM_ENABLED", "0")
    assert settings.detector_options().use_llm_claims is False


def test_предфильтр_отсекает_по_черновому_скору(make_vacancy) -> None:
    profile = score.Profile(skills=["python", "asyncio"], min_score=45.0)
    weak = make_vacancy(external_id="2", title="Курьер", skills=[], description="Развозить заказы")

    class FakeClient:
        def search(self, **kwargs):
            yield from (make_vacancy(external_id="1"), weak)

    strict = settings.PrefilterOptions(enabled=True, min_score=40.0, fuzzy=88)
    loose = settings.PrefilterOptions(enabled=False, min_score=40.0, fuzzy=88)
    profile.queries = [{"text": "python"}]

    _, passed = run.collect(FakeClient(), profile, 0, strict)
    assert [v.external_id for v in passed.values()] == ["1"]

    _, all_seen = run.collect(FakeClient(), profile, 0, loose)
    assert len(all_seen) == 2


def test_порог_истории_меняет_вердикт_детектора() -> None:
    claim = "Стабильная команда, текучки нет"
    vacancy = {"title": "Python разработчик", "description": claim}
    hist = detector.History(republished=4, days_tracked=20, snapshots=5, dated_snapshots=3)

    detector.configure(detector.Limits(min_days=30))
    try:
        report = detector.assess(vacancy, hist)
        assert report.flags == ()  # истории мало — вывода нет [HRD-004]

        detector.configure(detector.Limits(min_days=14))
        assert "stable_team" in detector.assess(vacancy, hist).flags
    finally:
        detector.configure(detector.Limits())
