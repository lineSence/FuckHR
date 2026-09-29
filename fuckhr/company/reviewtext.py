"""Страница отзывов → плоский текст: снять разметку и отличить отзыв от меню.

Нижний уровень разбора отзывов: его зовут и reviewitems.py (разбор на
отдельные отзывы), и reviewpage.py (чтение страниц), поэтому сам он не
импортирует ни того, ни другого.
"""

from __future__ import annotations

import html as html_mod
import re

from fuckhr.text import injection

MIN_LINE_CHARS = 40
# Блоки, внутри которых текста отзывов не бывает никогда.
DROP_BLOCK_RE = re.compile(
    r"<(script|style|noscript|svg|template|head|nav|footer|form|select|aside)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Границы абзацев: без них весь текст страницы слипается в одну строку и
# отделить отзыв от меню становится нечем.
BREAK_RE = re.compile(
    r"</?(p|div|li|tr|td|h[1-6]|section|article|blockquote|br)\b[^>]*>",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACES_RE = re.compile(r"[ \t\u00a0]+")
# Строки обвязки сайта. Проверяется вхождение в нижнем регистре.
BOILERPLATE = (
    "cookie", "куки", "политика конфиденциальн", "все права защищены",
    "пользовательское соглашение", "подпишитесь", "подписаться на рассылку",
    "войти через", "зарегистрироваться", "оставьте отзыв", "оставить отзыв",
    "читайте отзывы сотрудников", "реклама", "мы используем",
    "нажимая кнопку", "вакансии компании", "добавить компанию",
)
# Признаки живого отзыва. Строка без них — почти наверняка описание сервиса.
REVIEW_HINTS = (
    "плюс", "минус", "работал", "работаю", "работала", "уволил", "уволен",
    "зарплат", "оклад", "преми", "руководств", "начальник", "директор",
    "коллектив", "команд", "офис", "переработ", "график", "отпуск",
    "собеседован", "испытательн", "проект", "задач", "рекомендую",
    "сотрудник", "текучк", "обещал", "платят", "выплат",
)


def strip_tags(page: str) -> str:
    """HTML → плоский текст с сохранением границ абзацев.

    Скрытые вёрсткой блоки снимаются первыми: после снятия тегов текст
    «белым по белому» неотличим от обычного, а прячут в нём инструкции для
    ИИ-ассистента (ADR-020).
    """
    visible, _hidden = injection.drop_hidden(page or "")
    text = DROP_BLOCK_RE.sub(" ", visible)
    text = BREAK_RE.sub("\n", text)
    text = TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = SPACES_RE.sub(" ", text)
    return text


def looks_like_review(line: str) -> bool:
    """Похожа ли строка на кусок отзыва, а не на меню и не на рекламу."""
    low = line.lower()
    if len(line) < MIN_LINE_CHARS:
        return False
    if any(mark in low for mark in BOILERPLATE):
        return False
    if not any(hint in low for hint in REVIEW_HINTS):
        return False
    # Меню и хлебные крошки — это перечисления через разделители без точек.
    letters = sum(ch.isalpha() for ch in line)
    return letters >= len(line) * 0.5
