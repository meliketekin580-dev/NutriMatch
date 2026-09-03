"""Tarif servisinin Türkçe ve İngilizce metinler arasında çeviri yapmasına yardım eder.

Spoonacular çoğunlukla İngilizce malzeme ve talimat döndürdüğü için bu modül
kartlarda gösterilecek metni Türkçeleştirmeye, Türkçe kullanıcı girdisini de
API arama terimine dönüştürmeye çalışır. Çeviri paketi veya ağ yoksa orijinal
metin korunur; uygulama bu nedenle hata vermez.
"""

from functools import lru_cache
import re

from services.ai_service import gemini_error_message

# Çeviri paketi isteğe bağlıdır; yokluğu tarif uygulamasını durdurmamalıdır.
try:
    from deep_translator import GoogleTranslator
except ImportError:
    # Çeviri paketi kurulmamış olsa bile uygulamanın açılmasını engelleme.
    GoogleTranslator = None


@lru_cache(maxsize=256)
def translate_to_turkish(text: str) -> str:
    """İngilizce metni mümkünse Türkçeye çevirir.

    Args:
        text: Çevrilecek metin.

    Returns:
        str: Çevrilmiş metin veya çeviri yapılamazsa özgün metin.
    """
    if not text:
        return text
    # Gemini'nin ham servis hataları çeviri sağlayıcısına gönderilmez.
    # Böylece özel adın "İkizler" gibi yanlış çevrilmesi ve teknik ayrıntının
    # kullanıcıya sızması engellenir.
    normalized = str(text).casefold()
    gemini_error_markers = ("overloaded", "unavailable", "resource_exhausted")
    if "gemini" in normalized and any(marker in normalized for marker in gemini_error_markers):
        return gemini_error_message(response_text=text)
    if GoogleTranslator is None:
        return text
    try:
        # Gemini bir özel addır; çeviri servisine gönderilmez ve sonuçta aynen korunur.
        parts = re.split(r"(Gemini)", text, flags=re.IGNORECASE)
        translator = GoogleTranslator(source="en", target="tr")
        return "".join(
            "Gemini" if part.casefold() == "gemini" else (translator.translate(part) if part else "")
            for part in parts
        )
    except Exception:
        return text


@lru_cache(maxsize=256)
def translate_to_english(text: str) -> str:
    """Türkçe metni mümkünse İngilizceye çevirir.

    Args:
        text: Çevrilecek metin.

    Returns:
        str: Çevrilmiş metin veya çeviri yapılamazsa özgün metin.
    """
    if not text:
        return text
    if GoogleTranslator is None:
        return text
    try:
        return GoogleTranslator(source="tr", target="en").translate(text)
    except Exception:
        return text
