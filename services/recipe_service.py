"""Yerel tarifleri ve Spoonacular API sonuçlarını tek biçimde sunan servis.

Tarif arama akışı önce projedeki JSON kataloglarını kullanır; çevrim içi API
uygunsa sonuçları getirip yerel kataloğa ekleyebilir. Bu dosya malzeme uyumu,
besin hedefi filtresi, görsel düzeltmeleri ve API cevaplarının kartlarda
kullanılacak ortak sözlük biçimine dönüşmesini yönetir.
"""

from pathlib import Path
from typing import Any
import json

import requests
import streamlit as st

from services.translator import translate_to_english, translate_to_turkish
from utils.scoring import calculate_recipe_score, is_recipe_suitable_for_goal


# Spoonacular çağrılarının ortak temel adresi ve HTTP istekleri için üst süre sınırı.
BASE_URL = "https://api.spoonacular.com"
REQUEST_TIMEOUT = 15
# Yerel JSON dosyaları API kotası bittiğinde de tarif akışının çalışmasını sağlar.
LOCAL_CATALOG = Path(__file__).resolve().parent.parent / "data" / "recipes.json"
LOCAL_EXTRA_CATALOG = Path(__file__).resolve().parent.parent / "data" / "recipes_extra.json"
LOCAL_INGREDIENTS = Path(__file__).resolve().parent.parent / "data" / "recipe_ingredients.json"
LOCAL_IMAGE_OVERRIDES = Path(__file__).resolve().parent.parent / "data" / "recipe_image_overrides.json"

# Ana sayfadaki alan bir "rastgele popüler" listesi değil, daha güvenli bir
# sağlıklı ana yemek vitrini olmalı. Spoonacular'ın popülerlik sıralamasında
# zaman zaman tatlı, kanat veya yoğun soslu tarifler de yer alabiliyor.
HOME_RECIPE_EXCLUSIONS = (
    "cannoli", "ice cream", "dondurma", "chocolate", "çikolata", "cake",
    "cookie", "brownie", "cupcake", "dessert", "tatlı", "wing", "kanat",
    "deep fried", "fried", "kızarmış",
)


class RecipeServiceError(Exception):
    pass


def rank_recipe_candidates(
    recipes: list[dict[str, Any]],
    goal: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Tarif adaylarını hedef ve malzeme uyumuna göre filtreleyip sıralar.

    Args:
        recipes: Yerel veya API kaynağından gelen tarif sözlükleri.
        goal: Seçili beslenme hedefi.
        limit: Döndürülecek en yüksek tarif sayısı.

    Returns:
        list[dict[str, Any]]: Tekilleştirilmiş ve uygunluk sırasına konmuş tarifler.
    """
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Tekrar eden, hedefe uymayan veya yetersiz malzeme eşleşmesi olan adaylar elenir.
    for recipe in recipes:
        name = str(recipe.get("name", "")).strip()
        identity = name.casefold() or str(recipe.get("id", "")).strip().casefold()
        calories = float(recipe.get("calories", 0) or 0)
        match_ratio = float(recipe.get("match_ratio", 0) or 0)
        used = int(recipe.get("used", 0) or 0)
        total = int(recipe.get("total", 0) or 0)
        if not match_ratio and total:
            match_ratio = used / total
        if (
            not identity
            or identity in seen
            or calories <= 0
            or match_ratio < 0.4
            or not is_recipe_suitable_for_goal(calories, recipe.get("protein", 0), goal)
        ):
            continue
        normalized = dict(recipe)
        normalized["match_ratio"] = match_ratio
        unique.append(normalized)
        seen.add(identity)
    return sorted(
        unique,
        key=lambda recipe: (
            float(recipe.get("match_ratio", 0) or 0),
            int(recipe.get("used", 0) or 0),
            int(recipe.get("score", 0) or 0),
        ),
        reverse=True,
    )[:limit]


def load_local_recipes() -> list[dict[str, Any]]:
    """Yerel tarif kataloglarını okuyup görsel düzeltmeleriyle tekilleştirir.

    Args:
        Bu fonksiyon değer almaz.

    Returns:
        list[dict[str, Any]]: Yerel ve ek katalogdan gelen benzersiz tarifler.
    """
    try:
        with LOCAL_CATALOG.open(encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, list):
            return []
        try:
            # Varsa ek katalogdaki tarifler ana listeye eklenir.
            with LOCAL_EXTRA_CATALOG.open(encoding="utf-8") as file:
                extra = json.load(file)
            if isinstance(extra, list):
                data.extend(extra)
        except (OSError, ValueError):
            pass
        try:
            with LOCAL_IMAGE_OVERRIDES.open(encoding="utf-8") as file:
                image_overrides = json.load(file)
            if not isinstance(image_overrides, dict):
                image_overrides = {}
        except (OSError, ValueError):
            image_overrides = {}
        # Tarif adına ait özel görseller varsa katalog görselinin üzerine yazılır.
        for recipe in data:
            override_image = image_overrides.get(recipe.get("name"))
            if str(override_image or "").strip():
                recipe["image"] = override_image
        # Eski önbellek/katalog kayıtlarında aynı tarif birden fazla bulunabilir.
        # Tarif adı aynıysa bunu tek tarif kabul et.
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for recipe in data:
            identity = str(recipe.get("name", "")).strip().casefold()
            if not identity:
                identity = str(recipe.get("id", "")).strip().casefold()
            if identity and identity not in seen:
                seen.add(identity)
                unique.append(recipe)
        return unique
    except (OSError, ValueError):
        return []


def merge_ingredient_maps(
    primary: dict[str, list[str]],
    extra: dict[str, list[str]],
) -> dict[str, list[str]]:
    """İki malzeme sözlüğünü veri kaybetmeden birleştirir.

    Args:
        primary: Temel tarif-malzeme eşlemesi.
        extra: Aynı tariflerde öncelikli kabul edilen güncel eşleme.

    Returns:
        dict[str, list[str]]: Birleştirilmiş tarif-malzeme sözlüğü.
    """
    merged = dict(primary)
    merged.update(extra)
    return merged


def load_local_ingredients() -> dict[str, list[str]]:
    """Tek yerel JSON dosyasından tariflerin malzeme listelerini okur.

    Args:
        Bu fonksiyon değer almaz.

    Returns:
        dict[str, list[str]]: Tarif adına karşılık malzeme listesi veya boş sözlük.
    """
    try:
        with LOCAL_INGREDIENTS.open(encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, ValueError):
        return {}


def save_recipes_to_local(recipes: list[dict[str, Any]]) -> None:
    """API'den gelen yeni tarifleri yerel katalogya önbellek olarak kaydeder.

    Args:
        recipes: Kaydedilecek tarif sözlükleri.

    Returns:
        None: Tarifler yerel JSON'a yazılır; yazma hatası değer döndürmez.
    """
    current = load_local_recipes()
    known = {str(item.get("id") or item.get("name", "")).lower() for item in current}
    # Kimliği daha önce görülmeyen API tarifleri mevcut kataloğa eklenir.
    for recipe in recipes:
        identity = str(recipe.get("id") or recipe.get("name", "")).lower()
        if identity and identity not in known:
            cached = dict(recipe)
            cached["label"] = "Önbellek tarif"
            cached["show_missing"] = False
            current.append(cached)
            known.add(identity)
    try:
        LOCAL_CATALOG.parent.mkdir(parents=True, exist_ok=True)
        with LOCAL_CATALOG.open("w", encoding="utf-8") as file:
            json.dump(current, file, ensure_ascii=False, indent=2)
    except OSError:
        pass


def search_local_recipes(ingredients: list[str], goal: str) -> list[dict[str, Any]]:
    """Yerel tariflerde malzeme eşleşmesi arayıp hedefe göre sonuç döndürür.

    Args:
        ingredients: Kullanıcının verdiği malzeme adları.
        goal: Seçili beslenme hedefi.

    Returns:
        list[dict[str, Any]]: En az yüzde kırk malzeme uyumuna sahip tarifler.
    """
    aliases = {"kıyma": "et", "dana kıyma": "et", "zeytinyağı": "zeytinyağı", "domates": "domates"}
    terms = {aliases.get(item.strip().lower(), item.strip().lower()) for item in ingredients if item.strip()}
    # Labne, tarif eşleştirmesinde peynir grubunun bir üyesi olarak değerlendirilir.
    if "labne" in terms:
        terms.remove("labne")
        terms.add("peynir")
    if not terms:
        return []
    ingredient_hints = {
        "ızgara tavuk salata": {"tavuk", "salata", "domates", "salatalık"},
        "yulaflı meyveli pancake": {"yulaf", "muz", "yumurta", "süt"},
        "mercimek çorbası": {"mercimek", "soğan", "havuç"},
        "nohutlu buddha bowl": {"nohut", "salata", "avokado", "domates"},
        "sebzeli omlet": {"yumurta", "domates", "biber"},
        "ton balıklı sandviç": {"ton balığı", "ekmek", "marul", "domates"},
        "yoğurtlu meyve kasesi": {"yoğurt", "meyve", "muz"},
        "fırında sebzeli somon": {"somon", "sebze", "limon"},
        "tavuklu sebze sote": {"tavuk", "sebze", "biber"},
        "avokadolu kinoa salatası": {"avokado", "kinoa", "salata"},
        "sebzeli makarna": {"makarna", "domates", "sebze"},
        "fıstık ezmeli smoothie": {"fıstık ezmesi", "muz", "süt"},
        "bira - hamurlu kızarmış karides": {"karides", "bira hamuru", "tartar sosu"},
        "pastırma sarılmış fesleğen ve telliçeri biberli karides": {"karides", "pastırma", "fesleğen", "balzamik sirke"},
        "gambas al ajo": {"karides", "ekmek", "acı biber", "sarımsak"},
        "agedashi tofu": {"tofu", "mısır nişastası", "yeşil soğan", "hoisin sosu"},
        "karides ve patatas bravas": {"karides", "sarımsak", "maydanoz", "patates"},
    } | load_local_ingredients()
    matches = []
    seen_recipes: set[str] = set()
    # Her yerel tarif için kullanıcı malzemeleriyle kesişim ve hedef uygunluğu hesaplanır.
    for recipe in load_local_recipes():
        identity = str(recipe.get("id") or recipe.get("name", "")).strip().lower()
        if identity in seen_recipes:
            continue
        seen_recipes.add(identity)
        stored_ingredients = recipe.get("ingredients", [])
        hints = ({str(item).lower() for item in stored_ingredients} if stored_ingredients else set(ingredient_hints.get(str(recipe.get("name", "")).lower(), [])))
        used = len(terms & hints)
        match_ratio = used / len(terms) if terms else 0
        if used and match_ratio >= 0.4:
            item = dict(recipe)
            if not is_recipe_suitable_for_goal(item.get("calories", 0), item.get("protein", 0), goal):
                continue
            item["used"] = used
            item["total"] = len(hints)
            item["ingredients"] = sorted(hints)
            item["missing"] = sorted(hints - terms)
            item["show_missing"] = True
            item["score"] = calculate_recipe_score(
                item.get("calories", 0),
                item.get("protein", 0),
                item.get("carbohydrates", 0),
                item.get("fat", 0),
                used,
                len(hints),
                goal,
            )
            item["match_ratio"] = match_ratio
            matches.append(item)
    return rank_recipe_candidates(matches, goal)


def _api_key() -> str:
    """Streamlit gizli ayarlarından Spoonacular API anahtarını alır.

    Args:
        Bu yardımcı fonksiyon değer almaz.

    Returns:
        str: Kullanıma hazır API anahtarı.
    """
    try:
        key = st.secrets.get("SPOONACULAR_API_KEY", "")
    except Exception:
        key = ""
    if not key:
        raise RecipeServiceError(
            "Spoonacular API anahtarı bulunamadı. Anahtarı .streamlit/secrets.toml "
            "dosyasına eklemelisin."
        )
    return key


def _get_json(path: str, params: dict[str, Any]) -> Any:
    """Spoonacular uç noktasından JSON yanıtı alır ve servis hatalarını dönüştürür.

    Args:
        path: API temel adresine eklenecek uç nokta yolu.
        params: HTTP isteğine eklenecek sorgu parametreleri.

    Returns:
        Any: API'nin çözümlenmiş JSON yanıtı.
    """
    try:
        # Tarif servisine zaman aşımı sınırı olan GET isteği gönderilir.
        response = requests.get(
            f"{BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as error:
        raise RecipeServiceError("Tarif servisi zaman aşımına uğradı. Tekrar dene.") from error
    except requests.HTTPError as error:
        try:
            payload = error.response.json() if error.response is not None else {}
            message = payload.get("message", "") if isinstance(payload, dict) else ""
        except ValueError:
            message = ""
        if "daily points limit" in message.lower():
            raise RecipeServiceError("Spoonacular günlük API kotası doldu. Kota yenilendiğinde tekrar deneyebilirsin.") from error
        raise RecipeServiceError(message or "Tarif servisi isteği reddetti.") from error
    except (requests.RequestException, ValueError) as error:
        raise RecipeServiceError("Tarif servisine şu anda ulaşılamıyor.") from error

    if isinstance(payload, dict) and payload.get("status") == "failure":
        message = payload.get("message", "Tarif servisi isteği reddetti.")
        raise RecipeServiceError(message)
    return payload


def _nutrients(detail: dict[str, Any]) -> dict[str, float]:
    """Tarif ayrıntısından uygulamada kullanılan temel besin değerlerini çıkarır.

    Args:
        detail: Spoonacular tarif ayrıntısı sözlüğü.

    Returns:
        dict[str, float]: Kalori, protein, karbonhidrat ve yağ değerleri.
    """
    values = {"Calories": 0.0, "Protein": 0.0, "Carbohydrates": 0.0, "Fat": 0.0}
    for nutrient in detail.get("nutrition", {}).get("nutrients", []):
        name = nutrient.get("name")
        if name in values:
            values[name] = float(nutrient.get("amount", 0) or 0)
    return values


@st.cache_data(ttl=21600, show_spinner=False)
def get_popular_healthy_recipes() -> list[dict[str, Any]]:
    """Ana sayfa için filtrelenmiş popüler sağlıklı tarifleri API'den alır.

    Args:
        Bu fonksiyon değer almaz.

    Returns:
        list[dict[str, Any]]: Ana sayfa kartlarına uygun tarif sözlükleri.
    """
    # API isteğinde ana yemek, protein ve kalori filtreleri birlikte kullanılır.
    key = _api_key()
    payload = _get_json(
        "/recipes/complexSearch",
        {
            "apiKey": key,
            "number": 24,
            "sort": "popularity",
            "addRecipeNutrition": True,
            "instructionsRequired": True,
            "type": "main course",
            "minProtein": 12,
            "maxCalories": 550,
        },
    )

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RecipeServiceError("Popüler tarifler alınırken beklenmeyen bir cevap geldi.")

    recipes: list[dict[str, Any]] = []
    # Tatlı veya kızartma gibi vitrin dışı tarifler başlıklarına göre elenir.
    for item in payload["results"]:
        title = str(item.get("title", "")).strip()
        title_key = title.casefold()
        if not title or any(term in title_key for term in HOME_RECIPE_EXCLUSIONS):
            continue

        nutrition = _nutrients(item)
        recipes.append(
            {
                "id": item.get("id"),
                "name": translate_to_turkish(title),
                "score": calculate_recipe_score(
                    nutrition["Calories"],
                    nutrition["Protein"],
                    nutrition["Carbohydrates"],
                    nutrition["Fat"],
                    1,
                    1,
                    "Dengeli Beslenme",
                ),
                "calories": nutrition["Calories"],
                "protein": nutrition["Protein"],
                "carbohydrates": nutrition["Carbohydrates"],
                "fat": nutrition["Fat"],
                "used": 1,
                "total": 1,
                "missing": [],
                "image": item.get("image", ""),
                "source_url": item.get("sourceUrl", ""),
                "label": "API'den popüler",
                "detail": "sağlıklı tarif",
                "show_missing": False,
            }
        )

    return recipes


@st.cache_data(ttl=3600, show_spinner=False)
def search_recipes(ingredients: list[str], goal: str) -> list[dict[str, Any]]:
    """Spoonacular'da malzemelere göre arama yapıp hedefe uygun tarifler üretir.

    Args:
        ingredients: Kullanıcının Türkçe malzeme listesi.
        goal: Seçili beslenme hedefi.

    Returns:
        list[dict[str, Any]]: Besin, malzeme ve hazırlık bilgileriyle tarif listesi.
    """
    # API'nin arama uç noktası için malzeme adları İngilizceye çevrilir.
    key = _api_key()
    english_ingredients = [translate_to_english(item) for item in ingredients]
    matches = _get_json(
        "/recipes/findByIngredients",
        {
            "ingredients": ",".join(english_ingredients),
            "number": 5,
            "ranking": 2,
            "apiKey": key,
        },
    )

    if not isinstance(matches, list):
        raise RecipeServiceError("Tarif servisinden beklenmeyen bir cevap alındı.")

    results: list[dict[str, Any]] = []
    # Her arama sonucu için ayrıntılı tarif ve besin verisi ayrıca istenir.
    for match in matches:
        detail = _get_json(
            f"/recipes/{match.get('id')}/information",
            {"apiKey": key, "includeNutrition": True},
        )
        if not isinstance(detail, dict) or not detail.get("title"):
            continue

        used = int(match.get("usedIngredientCount", 0) or 0)
        missing = int(match.get("missedIngredientCount", 0) or 0)
        total = used + missing
        if english_ingredients and used / len(english_ingredients) < 0.4:
            continue

        nutrition = _nutrients(detail)
        if not is_recipe_suitable_for_goal(nutrition["Calories"], nutrition["Protein"], goal):
            continue
        missing_names = [
            translate_to_turkish(item.get("name", ""))
            for item in match.get("missedIngredients", [])
            if item.get("name")
        ]
        used_names = [
            translate_to_turkish(item.get("name", ""))
            for item in match.get("usedIngredients", [])
            if item.get("name")
        ]
        ingredient_name_fixes = {
            "kişisel tava": "pişirme spreyi",
            "kişisel pan": "pişirme spreyi",
            "personal pan": "pişirme spreyi",
            "cooking spray": "pişirme spreyi",
        }
        missing_names = [ingredient_name_fixes.get(name.strip().casefold(), name.strip().casefold()) for name in missing_names if name.strip()]
        missing_names = list(dict.fromkeys(missing_names))
        used_names = list(dict.fromkeys(name.strip().casefold() for name in used_names if name.strip()))
        ingredient_details = [
            translate_to_turkish(item.get("original", ""))
            for item in detail.get("extendedIngredients", [])
            if item.get("original")
        ]
        analyzed_steps: list[dict[str, Any]] = []
        # API'nin adım grupları, arayüzde gösterilecek tek adım listesine dönüştürülür.
        for section in detail.get("analyzedInstructions", []):
            for step in section.get("steps", []):
                analyzed_steps.append(
                    {
                        "number": step.get("number", len(analyzed_steps) + 1),
                        "step": translate_to_turkish(step.get("step", "")),
                        "ingredients": [
                            translate_to_turkish(item.get("name", ""))
                            for item in step.get("ingredients", [])
                            if item.get("name")
                        ],
                        "equipment": [
                            translate_to_turkish(item.get("name", ""))
                            for item in step.get("equipment", [])
                            if item.get("name")
                        ],
                    }
                )
        score = calculate_recipe_score(
            nutrition["Calories"],
            nutrition["Protein"],
            nutrition["Carbohydrates"],
            nutrition["Fat"],
            used,
            total,
            goal,
        )
        results.append(
            {
                "id": detail.get("id"),
                "name": translate_to_turkish(detail["title"]),
                "score": score,
                "calories": nutrition["Calories"],
                "protein": nutrition["Protein"],
                "carbohydrates": nutrition["Carbohydrates"],
                "fat": nutrition["Fat"],
                "used": used,
                "total": total,
                "match_ratio": used / len(english_ingredients) if english_ingredients else 0,
                "missing": missing_names,
                "ingredients": list(dict.fromkeys(used_names + missing_names)),
                "ingredient_details": ingredient_details,
                "image": detail.get("image", ""),
                "source_url": detail.get("sourceUrl", ""),
                "ready_in_minutes": detail.get("readyInMinutes"),
                "preparation_minutes": detail.get("preparationMinutes"),
                "cooking_minutes": detail.get("cookingMinutes"),
                "servings": detail.get("servings"),
                "instructions": detail.get("instructions", ""),
                "analyzed_steps": analyzed_steps,
                "label": "API önerisi",
                "detail": f"{used}/{total} malzeme uyumu",
                "show_missing": True,
            }
        )

    return sorted(results, key=lambda recipe: recipe["score"], reverse=True)
