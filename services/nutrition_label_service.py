import base64
import io
import json
import re
from typing import Any, Callable

import requests
import streamlit as st
from PIL import Image, ImageOps, UnidentifiedImageError

from services.ai_service import GEMINI_URL, _gemini_key


class NutritionLabelError(RuntimeError):
    """Besin etiketi analizinde kullanıcıya gösterilebilen kontrollü hata."""


LABEL_FIELDS = (
    "product_name",
    "basis_type",
    "serving_size",
    "energy_kj",
    "calories_kcal",
    "protein_g",
    "carbohydrates_g",
    "sugar_g",
    "fat_g",
    "saturated_fat_g",
    "fiber_g",
    "salt_g",
    "sodium_mg",
    "detected_text_summary",
    "unreadable_fields",
    "match_score",
    "positive_points",
    "attention_points",
    "goal_explanation",
)

NUMERIC_FIELDS = {
    "serving_size",
    "energy_kj",
    "calories_kcal",
    "protein_g",
    "carbohydrates_g",
    "sugar_g",
    "fat_g",
    "saturated_fat_g",
    "fiber_g",
    "salt_g",
    "sodium_mg",
    "match_score",
}

LIST_FIELDS = {"unreadable_fields", "positive_points", "attention_points"}


def resize_label_image(image_bytes: bytes, max_size: int = 1600) -> tuple[bytes, str]:
    """Görüntüyü oranını bozmadan küçültür ve API için JPEG'e dönüştürür."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=86, optimize=True)
            return output.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise NutritionLabelError("Fotoğraf okunamadı. Geçerli bir JPG, JPEG veya PNG dosyası yükle.") from exc


def _number_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace(" ", "").replace(",", ".")
    match = re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned)
    return float(cleaned) if match else None


def parse_label_response(raw_text: str) -> dict[str, Any]:
    """Gemini JSON çıktısını izin verilen alanlarla güvenli biçimde doğrular."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw_text).strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        raise NutritionLabelError("Yapay zekâ geçerli bir analiz sonucu döndürmedi. Fotoğrafı yeniden çekip deneyebilirsin.") from exc
    if not isinstance(parsed, dict):
        raise NutritionLabelError("Besin etiketi sonucu beklenen biçimde alınamadı.")

    result: dict[str, Any] = {field: None for field in LABEL_FIELDS}
    for field in LABEL_FIELDS:
        value = parsed.get(field)
        if field in NUMERIC_FIELDS:
            result[field] = _number_or_none(value)
        elif field in LIST_FIELDS:
            result[field] = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
        else:
            result[field] = str(value).strip() if value not in (None, "") else None

    basis = (result.get("basis_type") or "bilinmiyor").casefold()
    basis_aliases = {"100 g": "100 g", "100g": "100 g", "100 ml": "100 ml", "100ml": "100 ml", "porsiyon": "porsiyon"}
    result["basis_type"] = basis_aliases.get(basis, "bilinmiyor")
    for score_field in ("match_score",):
        score = result.get(score_field)
        result[score_field] = max(0.0, min(100.0, score)) if score is not None else None
    return result


def _response_schema() -> dict[str, Any]:
    nullable_number = {"type": "NUMBER", "nullable": True}
    nullable_string = {"type": "STRING", "nullable": True}
    string_list = {"type": "ARRAY", "items": {"type": "STRING"}}
    properties: dict[str, Any] = {
        "product_name": nullable_string,
        "basis_type": {"type": "STRING", "enum": ["100 g", "100 ml", "porsiyon", "bilinmiyor"]},
        "detected_text_summary": nullable_string,
        "goal_explanation": nullable_string,
        "unreadable_fields": string_list,
        "positive_points": string_list,
        "attention_points": string_list,
    }
    for field in NUMERIC_FIELDS:
        properties[field] = nullable_number
    return {"type": "OBJECT", "properties": properties, "required": list(LABEL_FIELDS)}


def _instruction(goal: str) -> str:
    return f"""
Bir paketli ürünün besin değerleri tablosunu analiz et. Türkçe yanıt ver.
Kullanıcının hedefi: {goal}

Kurallar:
1. Yalnızca etikette açıkça görülen değerleri oku; tahmin etme.
2. Okunamayan veya bulunmayan alanları null yap ve adını unreadable_fields listesine ekle.
3. 100 g, 100 ml ve porsiyon sütunlarını kesinlikle karıştırma. Çıkardığın sayılar yalnızca basis_type alanında belirttiğin aynı sütundan gelsin.
4. Birden fazla sütun varsa hangi sütunu kullandığını basis_type ile belirt; diğer sütunları detected_text_summary içinde kısaca açıkla.
5. Ondalık virgülü ve noktayı doğru sayıya dönüştür.
6. kJ ile kcal değerlerini ayrı alanlara yaz.
7. Tuz ile sodyumu birbirinin yerine kullanma.
8. Ürün adı görünmüyorsa product_name null olsun.
9. Fotoğraf bulanık, karanlık veya besin tablosu içermiyorsa okunamayan alanları belirt ve değerleri uydurma.
10. Tıbbi teşhis, tedavi veya kesin sağlık iddiası üretme.

{goal} hedefine göre kalori, porsiyon, protein, karbonhidrat, yağ, lif, şeker ve tuzu birlikte değerlendir.
Kilo Verme hedefinde tek değere bakarak kesin iyi/kötü deme.
Dengeli Beslenme hedefinde besin dengesi, çeşitlilik ve porsiyonun önemini belirt.
Kas Yapma hedefinde proteine önem ver ama kalori, şeker, yağ ve porsiyonu göz ardı etme.
match_score yaklaşık ve bilgilendirme amaçlı 0-100 NutriMatch uygunluk puanı olsun.
Yalnızca verilen JSON şemasına uygun cevap üret.
""".strip()


@st.cache_data(ttl=3600, show_spinner=False)
def analyze_nutrition_label(image_bytes: bytes, mime_type: str, goal: str) -> dict[str, Any]:
    """Gemini'ye tek istek göndererek yapılandırılmış etiket analizi döndürür."""
    api_key = _gemini_key()
    if not api_key:
        raise NutritionLabelError("Gemini API anahtarı bulunamadı. GEMINI_API_KEY ayarını kontrol et.")
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
        raise NutritionLabelError("Gemini servisine ulaşılamadı. İnternet bağlantını kontrol edip tekrar dene.") from exc
    if response.status_code in {429, 403}:
        raise NutritionLabelError("Gemini kotası dolmuş veya API anahtarı kullanılamıyor olabilir.")
    if response.status_code != 200:
        raise NutritionLabelError("Besin etiketi şu anda analiz edilemedi. Biraz sonra tekrar dene.")
    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise NutritionLabelError("Gemini'den geçerli bir analiz yanıtı alınamadı.") from exc
    return parse_label_response(raw_text)


def get_or_analyze_label(
    session_cache: dict[str, dict[str, Any]],
    photo_hash: str,
    goal: str,
    analyzer: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Aynı fotoğraf ve hedef için oturum içinde ikinci isteği engeller."""
    cache_key = f"{photo_hash}:{goal}"
    if cache_key not in session_cache:
        session_cache[cache_key] = analyzer()
    return session_cache[cache_key]
