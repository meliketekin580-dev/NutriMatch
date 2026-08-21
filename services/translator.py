from functools import lru_cache

try:
    from deep_translator import GoogleTranslator
except ImportError:
    # Çeviri paketi kurulmamış olsa bile uygulamanın açılmasını engelleme.
    GoogleTranslator = None


@lru_cache(maxsize=256)
def translate_to_turkish(text: str) -> str:
    if not text:
        return text
    if GoogleTranslator is None:
        return text
    try:
        return GoogleTranslator(source="en", target="tr").translate(text)
    except Exception:
        return text


@lru_cache(maxsize=256)
def translate_to_english(text: str) -> str:
    if not text:
        return text
    if GoogleTranslator is None:
        return text
    try:
        return GoogleTranslator(source="tr", target="en").translate(text)
    except Exception:
        return text
