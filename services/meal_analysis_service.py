import base64
import copy
import hashlib
import json
import re
from typing import Any, Callable

import requests
import streamlit as st

from services.ai_service import GEMINI_URL, _gemini_key
from services.nutrition_label_service import resize_label_image


class MealAnalysisError(RuntimeError):
    """Tabak analizinde kullanıcıya güvenli biçimde gösterilebilen hata."""


NUTRIENT_FIELDS = (
    "calories_kcal",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
)


def _number_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace(" ", "").replace(",", ".")
    return float(cleaned) if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned) else None


def parse_meal_response(raw_text: str) -> dict[str, Any]:
    """Gemini JSON çıktısını güvenli ve öngörülebilir bir yapıya dönüştürür."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw_text).strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MealAnalysisError("Yapay zekâ geçerli bir tabak analizi döndürmedi. Fotoğrafı yeniden deneyebilirsin.") from exc
    if not isinstance(parsed, dict):
        raise MealAnalysisError("Tabak analizi beklenen biçimde alınamadı.")

    items: list[dict[str, Any]] = []
    raw_items = parsed.get("items")
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
    """Gram değişikliklerini mevcut değerlere oranlar; Gemini çağrısı yapmaz."""
    if len(current_items) != len(edited_rows):
        raise MealAnalysisError("Düzenlenen ürün listesi analiz sonucuyla eşleşmiyor.")
    updated_items: list[dict[str, Any]] = []
    warnings: list[str] = []
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
    return bool(current_hash and previous_hash != current_hash)


def reset_meal_state_for_new_image(state: Any, current_hash: str) -> bool:
    """Yeni fotoğrafta yalnızca ekranda aktif olan eski analiz durumunu temizler."""
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
    cache_key = f"{photo_hash}:{goal}"
    if cache_key not in session_cache:
        session_cache[cache_key] = analyzer()
    return session_cache[cache_key]


def build_analysis_id(photo_hash: str, goal: str) -> str:
    return hashlib.sha256(f"{photo_hash}:{goal}".encode("utf-8")).hexdigest()


def add_daily_meal(daily_meals: list[dict[str, Any]], record: dict[str, Any]) -> bool:
    analysis_id = str(record.get("analysis_id") or "")
    if not analysis_id or any(str(item.get("analysis_id")) == analysis_id for item in daily_meals):
        return False
    daily_meals.append(copy.deepcopy(record))
    return True


def _response_schema() -> dict[str, Any]:
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
    api_key = _gemini_key()
    if not api_key:
        raise MealAnalysisError("Gemini API anahtarı bulunamadı. GEMINI_API_KEY ayarını kontrol et.")
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
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=35,
        )
    except requests.RequestException as exc:
        raise MealAnalysisError("Gemini servisine ulaşılamadı. İnternet bağlantını kontrol edip tekrar dene.") from exc
    if response.status_code in {403, 429}:
        raise MealAnalysisError("Gemini kotası dolmuş veya API anahtarı kullanılamıyor olabilir.")
    if response.status_code != 200:
        raise MealAnalysisError("Tabak şu anda analiz edilemedi. Biraz sonra tekrar dene.")
    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise MealAnalysisError("Gemini'den geçerli bir tabak analizi alınamadı.") from exc
    return parse_meal_response(raw_text)
