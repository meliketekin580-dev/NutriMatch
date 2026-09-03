"""Gemini ile konuşma, haftalık yorum ve fotoğraftan malzeme analizi yapan servis.

Bu dosya kullanıcı arayüzü oluşturmaz. ``ui.py`` tarafından iletilen soru,
tarif bağlamı veya görsel verisini Gemini API'sine gönderir ve uygulamanın
kullanabileceği sade sonuçlara dönüştürür. API anahtarı burada saklanmaz;
yalnızca Streamlit gizli ayarlarından okunur.
"""

import base64
import json
import logging
import re
from typing import Any

import requests
import streamlit as st


# Projedeki tüm Gemini istekleri aynı modelin ``generateContent`` uç noktasına gider.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"

logger = logging.getLogger(__name__)

GEMINI_OVERLOADED_MESSAGE = "Gemini hizmeti şu anda yoğun. Lütfen kısa bir süre sonra tekrar dene."
GEMINI_QUOTA_MESSAGE = "Gemini kullanım kotasına ulaşıldı. Lütfen daha sonra tekrar dene."
GEMINI_TIMEOUT_MESSAGE = "Gemini yanıt vermekte gecikti. Lütfen tekrar dene."
GEMINI_AUTH_MESSAGE = "Gemini API bağlantısı doğrulanamadı."
GEMINI_DEFAULT_ERROR_MESSAGE = "Fotoğraf şu anda analiz edilemedi. Lütfen tekrar dene."


class GeminiServiceError(RuntimeError):
    """Kullanıcıya yalnızca güvenli, önceden belirlenmiş Gemini hata mesajını taşır."""


def gemini_error_message(
    status_code: int | None = None,
    error: BaseException | None = None,
    response_text: str = "",
) -> str:
    """Gemini hatasını ham ayrıntıyı dışarı vermeden sabit Türkçe mesaja eşler.

    Args:
        status_code: Varsa Gemini HTTP durum kodu.
        error: Varsa bağlantı sırasında oluşan istisna.
        response_text: Yalnızca hata türünü belirlemek için incelenen ham yanıt.

    Returns:
        str: Arayüzde güvenle gösterilebilecek sabit Türkçe hata mesajı.
    """
    error_name = type(error).__name__ if error is not None else ""
    # Ham metin yalnızca sınıflandırma için bellekte incelenir; loglanmaz veya döndürülmez.
    normalized = f"{error_name} {error or ''} {response_text or ''}".casefold()
    if status_code == 503 or "overloaded" in normalized or "unavailable" in normalized:
        return GEMINI_OVERLOADED_MESSAGE
    if status_code == 429 or "resource_exhausted" in normalized:
        return GEMINI_QUOTA_MESSAGE
    if isinstance(error, requests.Timeout) or "timeout" in normalized or "timed out" in normalized:
        return GEMINI_TIMEOUT_MESSAGE
    if status_code in {401, 403}:
        return GEMINI_AUTH_MESSAGE
    return GEMINI_DEFAULT_ERROR_MESSAGE


def _safe_gemini_text(text: str) -> str:
    """Başarılı yanıt alanında hata metni geldiyse ham metni güvenli mesaja çevirir."""
    normalized = str(text or "").casefold()
    error_markers = ("overloaded", "unavailable", "resource_exhausted")
    if any(marker in normalized for marker in error_markers):
        return gemini_error_message(response_text=text)
    return str(text or "").strip()


def _gemini_key() -> str:
    """Streamlit gizli ayarlarından Gemini API anahtarını okur.

    Args:
        Bu yardımcı fonksiyon değer almaz.

    Returns:
        str: Anahtar varsa metin olarak, okunamazsa boş metin olarak döner.
    """
    try:
        return st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        return ""


def ask_gemini(prompt: str, recipes: list[dict[str, Any]], goal: str, ingredients: list[str]) -> str | None:
    """Tarif bağlamıyla Gemini'ye soru gönderip kısa yanıtı döndürür.

    Args:
        prompt: Kullanıcının sorduğu metin.
        recipes: Mevcut tarif sonuçları.
        goal: Seçili beslenme hedefi.
        ingredients: Kullanıcının malzeme listesi.

    Returns:
        str | None: Başarılı yanıtta metin, hata veya anahtar yoksa None.
    """
    # API anahtarı yalnızca Streamlit secrets üzerinden alınır.
    key = _gemini_key()
    if not key:
        return GEMINI_AUTH_MESSAGE
    first = recipes[0] if recipes else {}
    context = (
        f"Kullanıcının beslenme hedefi: {goal}\n"
        f"Elindeki malzemeler: {', '.join(ingredients) or 'belirtilmedi'}\n"
        f"İlk tarif: {first.get('name', 'belirtilmedi')}\n"
        f"Kalori: {first.get('calories', 0)}, protein: {first.get('protein', 0)} g\n"
    )
    instruction = "NutriMatch tarif asistanısın. Türkçe, kısa ve güvenli cevap ver. Sağlık teşhisi koyma.\n" + context + "\nKullanıcı sorusu: " + prompt
    try:
        # Gemini'ye tarif bağlamını içeren tek bir HTTP isteği gönderilir.
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": instruction}]}]},
            timeout=20,
        )
        if response.status_code != 200:
            logger.warning("Gemini metin isteği başarısız (status=%s)", response.status_code)
            return gemini_error_message(response.status_code, response_text=response.text)
        data = response.json()
        return _safe_gemini_text(data["candidates"][0]["content"]["parts"][0]["text"])
    except requests.Timeout as exc:
        logger.warning("Gemini metin isteği zaman aşımına uğradı")
        return gemini_error_message(error=exc)
    except requests.RequestException as exc:
        logger.warning("Gemini metin isteği başarısız (type=%s)", type(exc).__name__)
        return gemini_error_message(error=exc)
    except (ValueError, KeyError, IndexError, TypeError):
        return GEMINI_DEFAULT_ERROR_MESSAGE


def suggest_next_meal(goal: str, totals: dict[str, Any], meal_names: list[str]) -> str | None:
    """Günün toplamlarına göre Gemini'den yalnızca bir sonraki öğün önerisini alır.

    Args:
        goal: Seçili beslenme hedefi.
        totals: Günlük toplam besin değerleri.
        meal_names: Günlüğe eklenen öğün adları.

    Returns:
        str | None: Öneri metni veya alınamadığında None.
    """
    key = _gemini_key()
    if not key:
        return GEMINI_AUTH_MESSAGE

    summary = (
        f"Bugünkü kayıtlı öğünler: {', '.join(meal_names) or 'belirtilmedi'}\n"
        f"Toplam kalori: {totals.get('calories_kcal') or 'bilinmiyor'} kcal\n"
        f"Toplam protein: {totals.get('protein_g') or 'bilinmiyor'} g\n"
        f"Toplam karbonhidrat: {totals.get('carbohydrates_g') or 'bilinmiyor'} g\n"
        f"Toplam yağ: {totals.get('fat_g') or 'bilinmiyor'} g\n"
    )
    instruction = (
        "NutriMatch için Türkçe, kısa ve temkinli bir sonraki öğün önerisi üret. "
        "Tıbbi teşhis veya kesin diyet talimatı verme. Yalnızca tek bir sonraki öğün öner; "
        "1-2 cümlede nedenini açıkla. Kullanıcının hedefi: "
        f"{goal}.\n{summary}"
    )
    try:
        # Günlük özet, öneri üretmesi için Gemini isteğinin metnine eklenir.
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": instruction}]}]},
            timeout=20,
        )
        if response.status_code != 200:
            logger.warning("Gemini günlük öneri isteği başarısız (status=%s)", response.status_code)
            return gemini_error_message(response.status_code, response_text=response.text)
        data = response.json()
        return _safe_gemini_text(data["candidates"][0]["content"]["parts"][0]["text"])
    except requests.Timeout as exc:
        return gemini_error_message(error=exc)
    except requests.RequestException as exc:
        logger.warning("Gemini günlük öneri isteği başarısız (type=%s)", type(exc).__name__)
        return gemini_error_message(error=exc)
    except (ValueError, KeyError, IndexError, TypeError):
        return GEMINI_DEFAULT_ERROR_MESSAGE


def comment_on_weekly_summary(goal: str, weekly: dict[str, Any]) -> str | None:
    """Son yedi günün özetine göre Gemini'den genel yorum alır.

    Args:
        goal: Seçili beslenme hedefi.
        weekly: Gün ve ortalama değerleri içeren haftalık özet.

    Returns:
        str | None: Kısa yorum metni veya alınamadığında None.
    """
    key = _gemini_key()
    if not key:
        return GEMINI_AUTH_MESSAGE
    averages = weekly.get("averages") or {}
    instruction = (
        "NutriMatch için Türkçe, kısa ve temkinli haftalık beslenme yorumu üret. "
        "Tıbbi teşhis, kesin diyet programı veya kalori hedefi verme. Seçilen hedef ve "
        "son yedi takvim gününün yaklaşık günlük ortalamalarına dayanarak 2-3 cümle yaz. "
        "Protein, enerji ve genel dengeyi birlikte değerlendir; eksik veriler olabileceğini belirt.\n"
        f"Hedef: {goal}\n"
        f"Toplam kayıtlı öğün: {weekly.get('meal_count', 0)}\n"
        f"Günlük ortalama kalori: {averages.get('calories_kcal', 0)} kcal\n"
        f"Günlük ortalama protein: {averages.get('protein_g', 0)} g\n"
        f"Günlük ortalama karbonhidrat: {averages.get('carbohydrates_g', 0)} g\n"
        f"Günlük ortalama yağ: {averages.get('fat_g', 0)} g"
    )
    try:
        # Haftalık ortalamalar Gemini'ye tek HTTP isteğiyle gönderilir.
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": instruction}]}]},
            timeout=20,
        )
        if response.status_code != 200:
            logger.warning("Gemini haftalık yorum isteği başarısız (status=%s)", response.status_code)
            return gemini_error_message(response.status_code, response_text=response.text)
        data = response.json()
        return _safe_gemini_text(data["candidates"][0]["content"]["parts"][0]["text"])
    except requests.Timeout as exc:
        return gemini_error_message(error=exc)
    except requests.RequestException as exc:
        logger.warning("Gemini haftalık yorum isteği başarısız (type=%s)", type(exc).__name__)
        return gemini_error_message(error=exc)
    except (ValueError, KeyError, IndexError, TypeError):
        return GEMINI_DEFAULT_ERROR_MESSAGE


def analyze_food_image(image_bytes: bytes, mime_type: str) -> list[str] | None:
    """Fotoğrafta görünen yenilebilir malzemeleri Gemini ile tespit eder.

    Args:
        image_bytes: Yüklenen görselin ham baytları.
        mime_type: Görselin JPEG veya PNG türü.

    Returns:
        list[str] | None: Temizlenmiş malzeme adları veya analiz yapılamazsa None.
    """
    key = _gemini_key()
    if not key:
        raise GeminiServiceError(GEMINI_AUTH_MESSAGE)
    if not image_bytes or mime_type not in {"image/jpeg", "image/png"}:
        return None

    instruction = (
        "Bu fotoğrafı bir mutfak malzemesi tanıma sistemi olarak incele. "
        "Yalnızca fotoğrafta açıkça gördüğün yenilebilir yiyecek ve malzemelerin "
        "Türkçe, kısa ve tekil isimlerini döndür. Tahmin etme. Tabak, kase, bardak, "
        "ambalaj, etiket, masa, dolap, buzdolabı ve diğer mutfak eşyalarını dahil etme. "
        "Aynı malzemeyi birden fazla yazma. Yalnızca şu JSON biçiminde cevap ver: "
        '{"malzemeler":["domates","yumurta"]}. Hiç malzeme göremiyorsan boş liste döndür.'
    )
    # Görsel, API'nin beklediği biçim için Base64 metnine dönüştürülür.
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": instruction},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    try:
        # Görsel ve analiz yönergesi Gemini'ye birlikte gönderilir.
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if response.status_code != 200:
            logger.warning("Gemini malzeme fotoğrafı isteği başarısız (status=%s)", response.status_code)
            raise GeminiServiceError(
                gemini_error_message(response.status_code, response_text=response.text)
            )
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.IGNORECASE)
        parsed = json.loads(raw_text)
        values = parsed.get("malzemeler", []) if isinstance(parsed, dict) else []
        if not isinstance(values, list):
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        # Boş ve tekrar eden malzeme adları sonuç listesine eklenmez.
        for value in values:
            ingredient = re.sub(r"[^0-9A-Za-zÇĞİÖŞÜçğıöşü âîû-]", "", str(value)).strip().casefold()
            if ingredient and ingredient not in seen:
                seen.add(ingredient)
                cleaned.append(ingredient)
        return cleaned
    except GeminiServiceError:
        raise
    except requests.Timeout as exc:
        raise GeminiServiceError(gemini_error_message(error=exc)) from exc
    except requests.RequestException as exc:
        logger.warning("Gemini malzeme fotoğrafı isteği başarısız (type=%s)", type(exc).__name__)
        raise GeminiServiceError(gemini_error_message(error=exc)) from exc
    except (ValueError, KeyError, IndexError, TypeError):
        return None
