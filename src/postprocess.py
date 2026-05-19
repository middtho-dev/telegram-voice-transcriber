from __future__ import annotations

import re


DEFAULT_REPLACEMENTS = {
    "в п н": "VPN",
    "впн": "VPN",
    "телеграм": "Telegram",
    "айфон": "iPhone",
    "андроид": "Android",
    "виндовс": "Windows",
    "мак ос": "macOS",
    "макос": "macOS",
}

MILD_PROFANITY_REPLACEMENTS = {
    "бля": "блин",
    "блять": "блин",
    "сука": "черт",
    "хуй": "фиг",
    "пиздец": "капец",
}


def improve_transcript(
    text: str,
    *,
    custom_replacements: dict[str, str] | None = None,
    preserve_profanity: bool = True,
) -> str:
    improved = text.strip()
    if not improved:
        return improved

    improved = _remove_common_noise(improved)
    improved = _normalize_spacing(improved)
    improved = _apply_replacements(improved, DEFAULT_REPLACEMENTS)
    if custom_replacements:
        improved = _apply_replacements(improved, custom_replacements)
    if not preserve_profanity:
        improved = _apply_replacements(improved, MILD_PROFANITY_REPLACEMENTS)
    improved = _restore_sentence_punctuation(improved)
    improved = _capitalize_sentences(improved)
    return improved.strip()


def parse_replacements(value: str) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            continue
        source, target = line.split("=>", 1)
        source = source.strip()
        target = target.strip()
        if source and target:
            replacements[source] = target
    return replacements


def format_replacements(replacements: dict[str, str]) -> str:
    return "\n".join(f"{source} => {target}" for source, target in replacements.items())


def _remove_common_noise(text: str) -> str:
    patterns = (
        r"\bспасибо за просмотр\b",
        r"\bподписывайтесь на канал\b",
        r"\bсубтитры сделал[аи] .+?$",
    )
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _normalize_spacing(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,.!?;:])(?=\S)", r"\1 ", text)
    text = re.sub(r"([!?.,])\1{2,}", r"\1\1", text)
    return text.strip()


def _apply_replacements(text: str, replacements: dict[str, str]) -> str:
    result = text
    for source, target in replacements.items():
        result = re.sub(
            rf"(?<!\w){re.escape(source)}(?!\w)",
            target,
            result,
            flags=re.IGNORECASE,
        )
    return result


def _restore_sentence_punctuation(text: str) -> str:
    if re.search(r"[.!?]$", text):
        return text
    return f"{text}."


def _capitalize_sentences(text: str) -> str:
    chars = list(text)
    should_capitalize = True
    for index, char in enumerate(chars):
        if char.isalpha() and should_capitalize:
            chars[index] = char.upper()
            should_capitalize = False
        elif char in ".!?":
            should_capitalize = True
        elif not char.isspace():
            should_capitalize = False
    return "".join(chars)
