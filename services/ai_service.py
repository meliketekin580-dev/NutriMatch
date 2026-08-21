import base64
import json
import re
from typing import Any

import requests
import streamlit as st


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"


def _gemini_key() -> str:
    try:
        return st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        return ""


def ask_gemini(prompt: str, recipes: list[dict[str, Any]], goal: str, ingredients: list[str]) -> str | None:
    key = _gemini_key()
    if not key:
        return None
    first = recipes[0] if recipes else {}
    context = (
        f"Kullanıcının beslenme hedefi: {goal}\n"
        f"Elindeki malzemeler: {', '.join(ingredients) or 'belirtilmedi'}\n"
        f"İlk tarif: {first.get('name', 'belirtilmedi')}\n"
        f"Kalori: {first.get('calories', 0)}, protein: {first.get('protein', 0)} g\n"
    )
    instruction = "NutriMatch tarif asistanısın. Türkçe, kısa ve güvenli cevap ver. Sağlık teşhisi koyma.\n" + context + "\nKullanıcı sorusu: " + prompt
    try:
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": instruction}]}]},
            timeout=20,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None


def analyze_food_image(image_bytes: bytes, mime_type: str) -> list[str] | None:
    """Gemini ile fotoğrafta açıkça görülen yenilebilir malzemeleri bul."""
    key = _gemini_key()
    if not key or not image_bytes or mime_type not in {"image/jpeg", "image/png"}:
        return None

    instruction = (
        "Bu fotoğrafı bir mutfak malzemesi tanıma sistemi olarak incele. "
        "Yalnızca fotoğrafta açıkça gördüğün yenilebilir yiyecek ve malzemelerin "
        "Türkçe, kısa ve tekil isimlerini döndür. Tahmin etme. Tabak, kase, bardak, "
        "ambalaj, etiket, masa, dolap, buzdolabı ve diğer mutfak eşyalarını dahil etme. "
        "Aynı malzemeyi birden fazla yazma. Yalnızca şu JSON biçiminde cevap ver: "
        '{"malzemeler":["domates","yumurta"]}. Hiç malzeme göremiyorsan boş liste döndür.'
    )
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
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if response.status_code != 200:
            return None
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.IGNORECASE)
        parsed = json.loads(raw_text)
        values = parsed.get("malzemeler", []) if isinstance(parsed, dict) else []
        if not isinstance(values, list):
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            ingredient = re.sub(r"[^0-9A-Za-zÇĞİÖŞÜçğıöşü âîû-]", "", str(value)).strip().casefold()
            if ingredient and ingredient not in seen:
                seen.add(ingredient)
                cleaned.append(ingredient)
        return cleaned
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None
