"""Yüklenen tabak fotoğrafını Gemini ile analiz edip besin özetine dönüştürür.

Bu dosya fotoğrafın kendisini kullanıcıya göstermeden; görseli Gemini'ye uygun
biçime dönüştürür, dönen JSON'u güvenli şekilde temizler ve kullanıcı gram
miktarını değiştirdiğinde API çağrısı yapmadan değerleri orantılar. Aynı
fotoğrafın tekrar analiz edilmesini önlemek için hash ve session-state
anahtarları da burada yönetilir.
"""

import base64
import copy
import hashlib
import json
import logging
import re
from typing import Any, Callable

import requests
import streamlit as st

from services.ai_service import (
    GEMINI_AUTH_MESSAGE,
    GEMINI_DEFAULT_ERROR_MESSAGE,
    GEMINI_URL,
    _gemini_key,
    gemini_error_message,
)
from services.nutrition_label_service import resize_label_image


logger = logging.getLogger(__name__)


class MealAnalysisError(RuntimeError):
    """Tabak analizinde kullanıcıya güvenli biçimde gösterilebilen hata."""


def _raise_for_gemini_status(status_code: int, response_text: str = "") -> None:
    """Gemini HTTP durumunu kullanıcıya güvenli ve anlaşılır bir hataya çevirir."""
    if status_code != 200:
        # Yalnızca durum kodu loglanır; yanıt, anahtar ve görsel verisi yazılmaz.
        logger.warning("Tabak analizi Gemini HTTP hatası (status=%s)", status_code)
        raise MealAnalysisError(gemini_error_message(status_code, response_text=response_text))


# Toplam hesaplanırken ve gram değişikliğinde birlikte işlenen besin alanları.
NUTRIENT_FIELDS = (
    "calories_kcal",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
)


def _number_or_none(value: Any) -> float | None:
    """Sayısal değeri float'a çevirir; geçersiz veya belirsiz değerde None döndürür.

    Args:
        value: JSON ya da kullanıcı düzenlemesinden gelen değer.

    Returns:
        float | None: Dönüştürülmüş sayı veya None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace(" ", "").replace(",", ".")
    return float(cleaned) if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned) else None


def parse_meal_response(raw_text: str) -> dict[str, Any]:
    """Gemini JSON çıktısını güvenli ve öngörülebilir bir yapıya dönüştürür.

    Args:
        raw_text: Gemini'den gelen ham JSON metni.

    Returns:
        dict[str, Any]: Ürünleri, belirsizlikleri ve hedef yorumunu içeren analiz.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw_text).strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MealAnalysisError("Yapay zekâ geçerli bir tabak analizi döndürmedi. Fotoğrafı yeniden deneyebilirsin.") from exc
    if not isinstance(parsed, dict):
        raise MealAnalysisError("Tabak analizi beklenen biçimde alınamadı.")

    items: list[dict[str, Any]] = []
    raw_items = parsed.get("items")
    # Her geçerli ürün, sayısal alanları güvenle dönüştürülerek standart yapıya eklenir.
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            name = str(raw_item.get("name") or "").strip()
            item = {
                "name": name or "Adı okunamadı",
                "estimated_grams": _number_or_none(raw_item.get("estimated_grams")),
                "confidence": str(raw_item.get("confidence") or "düşük").strip().casefold(),
            }
            if item["confidence"] not in {"düşük", "orta", "yüksek"}:
                item["confidence"] = "düşük"
            for field in NUTRIENT_FIELDS:
                item[field] = _number_or_none(raw_item.get(field))
            items.append(item)

    overall_confidence = str(parsed.get("overall_confidence") or "düşük").strip().casefold()
    if overall_confidence not in {"düşük", "orta", "yüksek"}:
        overall_confidence = "düşük"
    uncertainties = parsed.get("uncertainties")
    return {
        "is_meal_image": parsed.get("is_meal_image") is True,
        "meal_name": str(parsed.get("meal_name")).strip() if parsed.get("meal_name") not in (None, "") else None,
        "items": items,
        "overall_confidence": overall_confidence,
        "goal_comment": str(parsed.get("goal_comment") or "").strip(),
        "uncertainties": [str(item).strip() for item in uncertainties if str(item).strip()] if isinstance(uncertainties, list) else [],
    }


def calculate_meal_totals(items: list[dict[str, Any]]) -> dict[str, float | None]:
    """Ürünlerdeki bilinen besin değerlerini toplayarak öğün toplamlarını hesaplar.

    Args:
        items: Analizde algılanan ürün sözlükleri.

    Returns:
        dict[str, float | None]: Her besin alanı için toplam değer veya None.
    """
    totals: dict[str, float | None] = {}
    for field in NUTRIENT_FIELDS:
        values = [_number_or_none(item.get(field)) for item in items]
        known_values = [value for value in values if value is not None]
        totals[field] = round(sum(known_values), 2) if known_values else None
    return totals


def scale_meal_items(
    current_items: list[dict[str, Any]],
    edited_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Gram değişikliklerini mevcut değerlere oranlar; Gemini çağrısı yapmaz.

    Args:
        current_items: Analizden gelen özgün ürünler.
        edited_rows: Kullanıcının ürün adı ve gram miktarı düzenlemeleri.

    Returns:
        tuple[list[dict[str, Any]], list[str]]: Güncellenen ürünler ve uyarılar.
    """
    if len(current_items) != len(edited_rows):
        raise MealAnalysisError("Düzenlenen ürün listesi analiz sonucuyla eşleşmiyor.")
    updated_items: list[dict[str, Any]] = []
    warnings: list[str] = []
    # Her düzenlenmiş satır, aynı sıradaki özgün analiz ürünüyle karşılaştırılır.
    for current, edited in zip(current_items, edited_rows):
        updated = copy.deepcopy(current)
        old_name = str(current.get("name") or "").strip()
        new_name = str(edited.get("Ürün adı") or "").strip()
        if not new_name:
            raise MealAnalysisError("Ürün adı boş bırakılamaz.")
        raw_grams = edited.get("Tahmini gram")
        new_grams = _number_or_none(raw_grams)
        if new_grams is None:
            raise MealAnalysisError(f"{new_name} için geçerli bir gram miktarı gir.")
        if new_grams <= 0:
            raise MealAnalysisError("Gram miktarı sıfırdan büyük olmalıdır.")

        old_grams = _number_or_none(current.get("estimated_grams"))
        updated["name"] = new_name
        updated["estimated_grams"] = new_grams
        if new_name.casefold() != old_name.casefold():
            warnings.append(f"{old_name or 'Ürün'} adı değiştirildi; mevcut besin değerleri yeni ad için doğrulanmış değildir.")
        if old_grams is None or old_grams <= 0:
            warnings.append(f"{new_name} için ilk gram değeri bilinmediğinden besin değerleri yeniden oranlanamadı.")
        else:
            ratio = new_grams / old_grams
            for field in NUTRIENT_FIELDS:
                old_value = _number_or_none(current.get(field))
                updated[field] = round(old_value * ratio, 2) if old_value is not None else None
        updated_items.append(updated)
    return updated_items, warnings


def is_new_meal_image(previous_hash: str | None, current_hash: str) -> bool:
    """Yeni görsel özetinin önceki görsel özetinden farklı olup olmadığını kontrol eder.

    Args:
        previous_hash: Önceki görselin özeti.
        current_hash: Yeni yüklenen görselin özeti.

    Returns:
        bool: Yeni ve boş olmayan bir görsel özeti varsa True.
    """
    return bool(current_hash and previous_hash != current_hash)


def reset_meal_state_for_new_image(state: Any, current_hash: str) -> bool:
    """Yeni fotoğrafta yalnızca ekranda aktif olan eski analiz durumunu temizler.

    Args:
        state: Streamlit session state benzeri anahtar-değer yapısı.
        current_hash: Yeni görselin içerik özeti.

    Returns:
        bool: Durum temizlendiyse True, görsel aynıysa False.
    """
    if not is_new_meal_image(state.get("meal_analysis_photo_hash"), current_hash):
        return False
    state["meal_analysis_photo_hash"] = current_hash
    for key in (
        "meal_analysis_active_result",
        "meal_analysis_error",
        "meal_edit_warnings",
        "meal_added_message",
    ):
        state.pop(key, None)
    return True


def get_or_analyze_meal(
    session_cache: dict[str, dict[str, Any]],
    photo_hash: str,
    goal: str,
    analyzer: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Aynı görsel ve hedef için analiz sonucunu oturum önbelleğinden döndürür.

    Args:
        session_cache: Oturum içinde tutulan analiz önbelleği.
        photo_hash: Görselin içerik özeti.
        goal: Seçili beslenme hedefi.
        analyzer: Önbellekte sonuç yokken analizi üreten işlev.

    Returns:
        dict[str, Any]: Önbellekteki veya yeni üretilen öğün analizi.
    """
    # Aynı anahtar bulunursa analyzer tekrar çağrılmaz.
    cache_key = f"{photo_hash}:{goal}"
    if cache_key not in session_cache:
        session_cache[cache_key] = analyzer()
    return session_cache[cache_key]


def build_analysis_id(photo_hash: str, goal: str) -> str:
    """Görsel özeti ve hedeften öğün kaydı için kararlı benzersiz kimlik üretir.

    Args:
        photo_hash: Görselin içerik özeti.
        goal: Seçili beslenme hedefi.

    Returns:
        str: SHA-256 ile üretilen analiz kimliği.
    """
    return hashlib.sha256(f"{photo_hash}:{goal}".encode("utf-8")).hexdigest()


def add_daily_meal(daily_meals: list[dict[str, Any]], record: dict[str, Any]) -> bool:
    """Yeni analiz kaydını günlük listeye ekler ve tekrarını engeller.

    Args:
        daily_meals: Oturumda tutulan günlük öğün listesi.
        record: Eklenecek öğün kaydı.

    Returns:
        bool: Yeni kayıt eklendiyse True, geçersiz veya tekrar ise False.
    """
    analysis_id = str(record.get("analysis_id") or "")
    if not analysis_id or any(str(item.get("analysis_id")) == analysis_id for item in daily_meals):
        return False
    daily_meals.append(copy.deepcopy(record))
    return True


def _response_schema() -> dict[str, Any]:
    """Gemini'nin döndürmesi beklenen tabak analizi JSON şemasını oluşturur.

    Args:
        Bu yardımcı fonksiyon değer almaz.

    Returns:
        dict[str, Any]: Structured output için şema sözlüğü.
    """
    nullable_number = {"type": "NUMBER", "nullable": True}
    item_properties: dict[str, Any] = {
        "name": {"type": "STRING"},
        "estimated_grams": nullable_number,
        "confidence": {"type": "STRING", "enum": ["düşük", "orta", "yüksek"]},
    }
    for field in NUTRIENT_FIELDS:
        item_properties[field] = nullable_number
    return {
        "type": "OBJECT",
        "properties": {
            "is_meal_image": {"type": "BOOLEAN"},
            "meal_name": {"type": "STRING", "nullable": True},
            "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": item_properties, "required": list(item_properties)}},
            "overall_confidence": {"type": "STRING", "enum": ["düşük", "orta", "yüksek"]},
            "goal_comment": {"type": "STRING"},
            "uncertainties": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["is_meal_image", "meal_name", "items", "overall_confidence", "goal_comment", "uncertainties"],
    }


def _instruction(goal: str) -> str:
    """Tabak fotoğrafı analizi için Gemini yönergesini hazırlar.

    Args:
        goal: Kullanıcının seçili beslenme hedefi.

    Returns:
        str: Gemini'ye gönderilecek talimat metni.
    """
    return f"""
Yalnızca bu istekte gönderilen güncel fotoğrafı analiz et. Önceki analizlerden hiçbir ürün taşıma.
Kullanıcının mevcut beslenme hedefi: {goal}

Kurallar:
- Yalnızca fotoğrafta görülebilen yenilebilir ürünleri belirle; görünmeyen ürünleri kesinmiş gibi ekleme.
- Her ürün için fotoğrafa dayalı yaklaşık gram, kalori, protein, karbonhidrat, yağ ve lif tahmini oluştur.
- Yağ, sos, şeker, tuz veya pişirme yönteminden emin değilsen uncertainties listesinde belirt.
- Tüm porsiyon ve besin değerlerinin yaklaşık olduğunu kabul et.
- Sayısal alanlarda yalnızca sayı kullan; emin olmadığın sayısal değerde null döndür.
- Görsel yemek içermiyorsa is_meal_image false, items boş liste olsun.
- Görsel çok bulanıksa overall_confidence düşük olsun ve uncertainties içinde daha net fotoğraf gerektiğini belirt.
- Ürün isimleri, öğün adı, belirsizlikler ve değerlendirme Türkçe olsun.
- Hedef yalnızca goal_comment değerlendirmesini etkilesin; fotoğrafta olmayan ürün eklenmesine neden olmasın.
- Goal comment kesin sağlık tavsiyesi vermesin; genel protein, enerji ve besin dengesini temkinli biçimde açıklasın.
- Yalnızca tanımlanan JSON şemasına uygun JSON döndür; Markdown kullanma.
""".strip()


@st.cache_data(ttl=3600, show_spinner=False)
def analyze_meal_image(image_bytes: bytes, mime_type: str, goal: str) -> dict[str, Any]:
    """Tabak fotoğrafını Gemini ile analiz edip doğrulanmış sonucu döndürür.

    Args:
        image_bytes: Yüklenen fotoğrafın ham baytları.
        mime_type: Görselin MIME türü.
        goal: Seçili beslenme hedefi.

    Returns:
        dict[str, Any]: Öğün ve besin değerleri analiz sonucu.
    """
    api_key = _gemini_key()
    if not api_key:
        raise MealAnalysisError(GEMINI_AUTH_MESSAGE)
    resized_bytes, resized_mime = resize_label_image(image_bytes)
    payload = {
        "contents": [{"parts": [
            {"text": _instruction(goal)},
            {"inline_data": {"mime_type": resized_mime, "data": base64.b64encode(resized_bytes).decode("ascii")}},
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": _response_schema(),
        },
    }
    try:
        # Görsel, yönerge ve JSON şeması Gemini'ye tek HTTP isteğiyle gönderilir.
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=35,
        )
    except requests.Timeout as exc:
        logger.warning("Tabak analizi Gemini bağlantı hatası (type=timeout)")
        raise MealAnalysisError(gemini_error_message(error=exc)) from exc
    except requests.RequestException as exc:
        # Hata metni URL veya istek ayrıntısı içerebileceğinden yalnızca sınıf adı loglanır.
        logger.warning("Tabak analizi Gemini bağlantı hatası (type=%s)", type(exc).__name__)
        raise MealAnalysisError(gemini_error_message(error=exc)) from exc
    _raise_for_gemini_status(response.status_code, response.text)
    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise MealAnalysisError(GEMINI_DEFAULT_ERROR_MESSAGE) from exc
    return parse_meal_response(raw_text)
