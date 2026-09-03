from html import escape
from base64 import b64encode
from datetime import date, datetime, timedelta
import hashlib
import logging
import json
import re
from pathlib import Path
from typing import Callable

import streamlit as st

from services.ai_service import (
    GeminiServiceError,
    analyze_food_image,
    ask_gemini,
    comment_on_weekly_summary,
    suggest_next_meal,
)
from services.daily_meal_store import (
    DailyMealStoreError,
    claim_legacy_daily_meals_for_user,
    count_claimable_legacy_daily_meals,
    delete_daily_meal,
    load_daily_meals,
    load_daily_meals_for_date,
    meals_for_date,
    save_daily_meal,
    weekly_summary,
)
from services.nutrition_label_service import (
    NutritionLabelError,
    analyze_nutrition_label,
    get_or_analyze_label,
)
from services.meal_analysis_service import (
    MealAnalysisError,
    add_daily_meal,
    analyze_meal_image,
    build_analysis_id,
    calculate_meal_totals,
    get_or_analyze_meal,
    reset_meal_state_for_new_image,
    scale_meal_items,
)
from services.translator import translate_to_turkish
from services.youtube_service import YouTubeServiceError, search_youtube_videos

logger = logging.getLogger(__name__)


GREEN = "#1f7a43"
GOAL_ICONS = {"Kilo Verme": "🌿", "Dengeli Beslenme": "⚖️", "Kas Yapma": "💪"}
HERO_IMAGE = "https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&w=900&q=85"
ABOUT_IMAGE = "https://images.unsplash.com/photo-1543362906-acfc16c67564?auto=format&fit=crop&w=900&q=85"
FEATURED_RECIPES = [
    {
        "name": "Izgara Tavuk Salata",
        "image": "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=700&q=85",
        "score": 88,
        "calories": 420,
        "protein": 32,
        "used": 4,
        "total": 5,
        "missing": ["Yoğurt sos"],
        "label": "Popüler seçenek",
        "detail": "yüksek protein",
        "show_missing": False,
    },
    {
        "name": "Yulaflı Meyveli Pancake",
        "image": "https://images.unsplash.com/photo-1528207776546-365bb710ee93?auto=format&fit=crop&w=700&q=85",
        "score": 82,
        "calories": 310,
        "protein": 13,
        "used": 3,
        "total": 5,
        "missing": ["Yaban mersini", "Bal"],
        "label": "Popüler seçenek",
        "detail": "kahvaltı",
        "show_missing": False,
    },
    {
        "name": "Mercimek Çorbası",
        "image": "https://images.unsplash.com/photo-1547592166-23ac45744acd?auto=format&fit=crop&w=700&q=85",
        "score": 85,
        "calories": 280,
        "protein": 16,
        "used": 4,
        "total": 6,
        "missing": ["Limon"],
        "label": "Popüler seçenek",
        "detail": "hafif akşam yemeği",
        "show_missing": False,
    },
]

RECIPE_IMAGE_FALLBACKS = {
    "yumurta": "static/images/sebzeli_omlet.png",
    "omlet": "static/images/sebzeli_omlet.png",
    "menemen": "static/images/sebzeli_menemen.png",
    "sote": "static/images/sebzeli_menemen.png",
    "güveç": "static/images/sebzeli_menemen.png",
    "kabak": "static/images/firinda_kabak_mucver.png",
    "mücver": "static/images/firinda_kabak_mucver.png",
    "salata": "static/images/kisir.png",
    "kısır": "static/images/kisir.png",
}
DEFAULT_RECIPE_IMAGE = "static/images/kisir.png"


@st.cache_data(show_spinner=False)
def _image_source(source: str) -> str:
    """Yerel görselleri HTML kartlarında kullanılabilen data URL'ye çevir."""
    source = str(source or "").strip()
    if source.startswith("static/"):
        path = Path(__file__).parent / source
        try:
            encoded = b64encode(path.read_bytes()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except OSError:
            return ""
    return source


def _fallback_recipe_image(recipe: dict) -> str:
    """Tarif adına en yakın yerel yedek görselin data URL'sini döndürür."""
    recipe_name = str(recipe.get("name", "")).casefold()
    fallback_path = next(
        (path for keyword, path in RECIPE_IMAGE_FALLBACKS.items() if keyword in recipe_name),
        DEFAULT_RECIPE_IMAGE,
    )
    return _image_source(fallback_path)


def _recipe_image_sources(recipe: dict) -> tuple[str, str]:
    """Tarif için birincil görseli ve yükleme hatasında kullanılacak yedeği döndürür."""
    fallback = _fallback_recipe_image(recipe)
    primary = _image_source(str(recipe.get("image", ""))) or fallback
    return primary, fallback


def get_local_recipes() -> list[dict]:
    catalog_path = Path(__file__).parent / "data" / "recipes.json"
    extra_catalog_path = Path(__file__).parent / "data" / "recipes_extra.json"
    ingredients_path = Path(__file__).parent / "data" / "recipe_ingredients.json"
    api_details_path = Path(__file__).parent / "data" / "api_recipe_details_tr.json"
    image_overrides_path = Path(__file__).parent / "data" / "recipe_image_overrides.json"
    try:
        with catalog_path.open(encoding="utf-8") as file:
            recipes = json.load(file)
        try:
            with extra_catalog_path.open(encoding="utf-8") as file:
                recipes.extend(json.load(file))
        except (OSError, ValueError):
            pass
        try:
            with ingredients_path.open(encoding="utf-8") as file:
                ingredients = json.load(file)
        except (OSError, ValueError):
            ingredients = {}
        try:
            with api_details_path.open(encoding="utf-8") as file:
                api_details = json.load(file)
        except (OSError, ValueError):
            api_details = {}
        try:
            with image_overrides_path.open(encoding="utf-8") as file:
                image_overrides = json.load(file)
        except (OSError, ValueError):
            image_overrides = {}
        for recipe in recipes:
            mapped_ingredients = ingredients.get(str(recipe.get("name", "")).lower())
            if mapped_ingredients:
                recipe["ingredients"] = mapped_ingredients
            elif "ingredients" not in recipe:
                recipe["ingredients"] = []
            if recipe.get("id") and str(recipe["id"]) in api_details:
                recipe.update(api_details[str(recipe["id"])])
            override_image = image_overrides.get(recipe.get("name"))
            if str(override_image or "").strip():
                recipe["image"] = override_image
        unique: list[dict] = []
        seen: set[str] = set()
        for recipe in recipes:
            identity = str(recipe.get("id") or recipe.get("name", "")).strip().lower()
            if identity and identity not in seen:
                seen.add(identity)
                unique.append(recipe)
        return unique
    except (OSError, ValueError):
        return FEATURED_RECIPES


def get_chatbot_knowledge() -> dict:
    path = Path(__file__).parent / "data" / "chatbot_knowledge.json"
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return {"alternatives": {}, "tips": {}}


def get_recipe_instructions(name: str) -> list[str]:
    path = Path(__file__).parent / "data" / "recipe_instructions.json"
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
        return data.get(name.lower(), [])
    except (OSError, ValueError):
        return []


def apply_styles() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"], [data-testid="stHeader"], #MainMenu, footer {display:none !important;}
        .stApp {background:#fbfcfa; color:#10233f;}
        .block-container,
        [data-testid="stMainBlockContainer"] {
            box-sizing:border-box !important;
            width:92% !important;
            max-width:1240px !important;
            padding:24px 24px 64px !important;
            margin:0 auto !important;
        }
        .topbar {display:flex; align-items:center; justify-content:space-between; min-height:72px; margin-top:0;}
        .nav-divider {height:1px; background:#e5e7eb; margin:10px 0 28px;}
        .brand {display:flex; align-items:center; gap:9px; min-height:52px; font-size:25px; color:#08783f; font-weight:900; line-height:32px; padding:5px 0; white-space:nowrap; letter-spacing:-.5px;}
        .brand-mark {display:inline-flex; align-items:center; justify-content:center; width:40px; height:38px; color:#08783f;}
        .brand-mark svg {width:40px; height:38px; overflow:visible;}
        .brand-nutri {color:#08783f;}
        .brand-match {color:#f5a313;}
        [data-testid="stHorizontalBlock"]:has(.brand) {align-items:center;}
        .st-key-navbar_profile {width:100%;}
        .st-key-navbar_profile [data-testid="stPopover"] > button {
            min-height:46px !important; width:100% !important; padding:8px 13px !important;
            justify-content:flex-start !important; overflow:hidden !important;
            color:#0b5f37 !important; background:#f5fbf6 !important;
            border:2px solid #c8dfcf !important; border-radius:999px !important;
            box-shadow:0 3px 8px rgba(15,23,42,.06) !important;
        }
        .st-key-navbar_profile [data-testid="stPopover"] > button p {
            min-width:0 !important; overflow:hidden !important; text-overflow:ellipsis !important;
            white-space:nowrap !important; color:#0b5f37 !important;
            font-size:14px !important; font-weight:800 !important;
        }
        .st-key-navbar_profile [data-testid="stPopover"] > button:hover {
            color:#064e2e !important; border-color:#78b98d !important; background:#edf8f0 !important;
        }
        [data-testid="stPopoverBody"]:has(.profile-menu-name) {
            width:min(280px,calc(100vw - 24px)) !important; padding:14px !important;
            border:1px solid #d7e6db !important; border-radius:16px !important;
            background:#fffdfa !important; box-shadow:0 14px 32px rgba(15,23,42,.14) !important;
        }
        .profile-menu-name {margin:0 0 10px; color:#10233f; font-size:14px; font-weight:800; overflow-wrap:anywhere;}
        .st-key-oidc_logout button {min-height:40px !important; color:#fff !important; background:#1f7a43 !important; border-color:#1f7a43 !important;}
        h1, h2, h3 {color:#071a38 !important; letter-spacing:0;}
        [data-testid="stHorizontalBlock"]:has(.home-hero-copy) {height:344px; min-height:344px; align-items:stretch; gap:0; margin:2px 0 18px; padding:0; overflow:hidden; border:1px solid #0b5a3d; border-radius:18px; background:#034d34 center/cover no-repeat; box-shadow:0 14px 36px rgba(15,23,42,.12);}
        [data-testid="stHorizontalBlock"]:has(.home-hero-copy) > [data-testid="stColumn"]:first-child {display:flex; flex-direction:column; justify-content:center; padding:32px 34px 30px 38px; background:linear-gradient(90deg,rgba(0,57,38,.99),rgba(0,77,49,.92) 68%,rgba(0,77,49,0));}
        [data-testid="stHorizontalBlock"]:has(.home-hero-copy) > [data-testid="stColumn"]:last-child {min-height:0; padding:0;}
        .home-hero-copy {display:block; width:1px; height:1px; overflow:hidden; opacity:0; pointer-events:none;}
        .eyebrow {display:inline-flex; width:fit-content; align-items:center; padding:8px 13px; border-radius:999px; background:rgba(34,197,94,.17); border:1px solid rgba(134,239,172,.22); color:#f0fff5; font-size:14px; font-weight:800; margin-bottom:9px;}
        .hero-title {font-size:45px; line-height:1.14; font-weight:850; color:#fff; margin:9px 0 12px; letter-spacing:-1.1px;}
        .hero-title span {color:#55d56b;}
        .muted {color:#526779; line-height:1.7;}
        .hero-title + .muted {max-width:520px; color:#eef9f2 !important; font-size:16px; line-height:1.6; font-weight:600; margin:0 0 16px;}
        .hero-description {max-width:520px; color:#ffffff !important; font-size:16px !important; line-height:1.55 !important; font-weight:650 !important; letter-spacing:.05px; margin:0 0 14px !important; text-shadow:0 1px 2px rgba(0,0,0,.34);}
        .hero-image-frame {display:none;}
        .hero-image-frame .hero-image {display:block; width:100% !important; max-width:none !important; height:100% !important; object-fit:cover; object-position:center; border-radius:0 21px 21px 0; box-shadow:none;}
        .hero-image {display:block; width:100% !important; max-width:none !important; height:405px; object-fit:cover; object-position:center; border-radius:22px; box-shadow:0 16px 35px rgba(15,23,42,.13);}
        .hero-proof {display:none;}
        .hero-proof span:nth-child(1) {background:#e9f7ee; border-color:#86c59a;}
        .hero-proof span:nth-child(2) {background:#fff6e3; border-color:#f0c36b; color:#714b0d;}
        .hero-proof span:nth-child(3) {background:#f0f8ed; border-color:#a9d59b;}
        .hero-proof b {color:#147a3c; font-size:18px; line-height:1; font-weight:850;}
        .hero-proof span:nth-child(2) b {color:#d97706;}
        .section {padding:28px 0 16px;}
        .center {text-align:center;}
        .popular-heading {text-align:center; padding:34px 0 24px;}
        .popular-heading h2 {font-size:32px; font-weight:850; margin:0 auto 10px; display:flex; align-items:center; justify-content:center; gap:22px;}
        .popular-heading h2::before, .popular-heading h2::after {content:""; display:inline-block; width:58px; height:1px; background:#b8ddc4;}
        .popular-heading h2::after {transform:rotate(0deg);}
        .info-card, .recipe-card, .stat-card {background:#fff; border:1px solid #e5e7eb; border-radius:16px; box-shadow:0 5px 16px rgba(15,23,42,.06);}
        .info-card {position:relative; padding:24px 20px; text-align:left; min-height:112px;}
        .home-feature-heading {text-align:center; padding:0 0 16px;}
        .home-feature-heading h2 {display:flex; align-items:center; justify-content:center; gap:20px; margin:0; font-size:27px; font-weight:850;}
        .home-feature-heading h2::before,.home-feature-heading h2::after {content:""; width:42px; height:2px; background:#59b77b;}
        .home-feature-card {position:relative; min-height:145px; height:100%; padding:17px 21px; text-align:center; border:1px solid #e1e8e3; border-radius:14px; background:#fff; box-shadow:0 5px 15px rgba(15,23,42,.06);}
        .home-feature-card .feature-icon {display:flex; align-items:center; justify-content:center; width:46px; height:46px; margin:0 auto 8px; border-radius:50%; background:#edf8ef; color:#178042; font-size:25px;}
        .home-feature-card h4 {margin:0 0 6px; color:#091d42; font-size:17px; font-weight:820;}
        .home-feature-card p {max-width:260px; margin:0 auto; color:#425b78; font-size:14px; line-height:1.45;}
        .home-feature-card--wide {display:flex; align-items:center; min-height:102px; padding:18px 28px; text-align:left;}
        .home-feature-card--wide .feature-icon {flex:0 0 48px; margin:0 18px 0 0;}
        .home-feature-card--wide p {max-width:none; margin:0;}
        .home-step-card {min-height:165px; height:100%; margin:0; padding:25px 20px;}
        .home-step-card .step-content {min-height:122px; gap:18px;}
        .home-step-card .info-icon {width:58px; height:58px; flex-basis:58px;}
        .home-step-card .info-icon svg {width:31px; height:31px;}
        .step-number {position:absolute; left:18px; top:-13px; display:flex; align-items:center; justify-content:center; width:27px; height:27px; border-radius:50%; background:#2f9e44; color:#fff; font-size:12px; font-weight:800;}
        .step-content {display:flex; align-items:center; gap:16px; min-height:76px;}
        .info-icon {display:inline-flex; flex:0 0 48px; align-items:center; justify-content:center; width:48px; height:48px; border-radius:50%; background:#eef8e8; color:#1f7a43; font-size:22px;}
        .info-icon svg {width:28px; height:28px;}
        .info-card h4 {margin:0 0 8px; color:#10233f; font-size:20px; font-weight:820;}
        .info-card p {margin:0; font-size:15px; line-height:1.55; font-weight:520;}
        .recipe-card {overflow:hidden; margin-bottom:10px; min-height:390px; border-radius:16px; transition:transform .18s ease, box-shadow .18s ease;}
        .recipe-card:hover {transform:translateY(-3px); box-shadow:0 12px 24px rgba(15,23,42,.1);}
        .recipe-card img {width:100%; height:235px; min-height:235px; max-height:235px; object-fit:cover; display:block;}
        .recipe-card.home-recipe-card {display:flex; flex-direction:column; min-height:215px; height:215px; margin-bottom:10px;}
        .home-recipe-card img,.home-recipe-card .recipe-image-placeholder {width:100%; height:94px; min-height:94px;}
        .home-recipe-card .recipe-body {display:flex; flex:1; flex-direction:column; min-width:0; padding:11px 16px 12px;}
        .home-recipe-card .recipe-type {display:none;}
        .home-recipe-card .recipe-name {min-height:43px; font-size:15px; margin-bottom:7px;}
        .home-recipe-card .recipe-meta {gap:9px; margin-top:auto; font-size:13px;}
        .home-recipe-card .goal-pill {display:none;}
        .recipe-image-placeholder {height:235px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:9px; background:linear-gradient(145deg,#f3f8f4,#e8f2eb); color:#1f7a43;}
        .recipe-image-placeholder svg {width:42px; height:42px;}
        .recipe-image-placeholder span {font-size:12px; color:#64748b; font-weight:650;}
        .recipe-body {padding:16px 17px 18px;}
        .recipe-type {font-size:13px; color:#718096; margin-bottom:7px; font-weight:600;}
        .recipe-name {font-size:17px; line-height:1.45; font-weight:800; color:#10233f; min-height:50px;}
        .recipe-meta {display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-top:12px;}
        .goal-pill {display:inline-block; margin:0 0 0 auto; padding:5px 9px; border-radius:99px; background:#e9f7ee; color:#1f7a43; font-size:11px; font-weight:700;}
        .recipe-card--library .goal-pill {display:none !important;}
        .detail-photo {width:100%; height:520px; object-fit:cover; border-radius:20px; display:block; box-shadow:0 12px 30px rgba(15,23,42,.11);}
        .detail-photo-placeholder {height:520px; border-radius:20px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; background:linear-gradient(145deg,#f3f8f4,#e8f2eb); border:1px solid #dce9df; color:#1f7a43;}
        .detail-photo-placeholder svg {width:72px; height:72px;}
        .detail-photo-placeholder span {color:#64748b; font-weight:650;}
        .detail-panel {background:#fff; border:1px solid #e2e8e4; border-radius:20px; padding:28px; box-shadow:0 8px 24px rgba(15,23,42,.07);}
        .detail-panel h1 {font-size:38px; line-height:1.15; margin:0 0 6px;}
        .detail-kicker {color:#64748b; font-size:13px; margin-bottom:22px;}
        .nutrition-grid {display:grid; grid-template-columns:1fr 1fr; gap:12px;}
        .nutrition-box {background:#f7faf8; border:1px solid #e2ebe5; border-radius:14px; padding:15px 16px;}
        .nutrition-box span {display:block; color:#64748b; font-size:12px; margin-bottom:5px;}
        .nutrition-box strong {color:#071a38; font-size:23px; line-height:1.2;}
        .missing-panel {margin-top:18px; padding:15px 16px; background:#fff7ed; border:1px solid #fed7aa; border-radius:14px;}
        .missing-panel strong {display:block; color:#9a3412; font-size:14px; margin-bottom:8px;}
        .missing-tag {display:inline-block; margin:3px 5px 3px 0; padding:5px 9px; border-radius:999px; background:#fff; border:1px solid #fdba74; color:#9a3412; font-size:12px;}
        .complete-panel {margin-top:18px; padding:14px 16px; background:#ecfdf3; border:1px solid #a7e0bb; border-radius:14px; color:#166534; font-weight:650;}
        .nutrition {font-size:12px; color:#64748b; margin-top:0; white-space:nowrap;}
        .meta-icon {color:#1f7a43; font-weight:800; font-size:14px;}
        .stat-card {padding:26px; text-align:center;}
        .stat-card strong {display:block; color:#1f7a43; font-size:27px;}
        .missing {font-size:12px; color:#9a3412; margin-top:8px;}
        .ingredient-summary {margin:14px 0 22px; padding:14px; background:#e9f7ee; border:1px solid #b8ddc4; border-radius:14px;}
        .ingredient-summary-title {display:block; color:#1f7a43; font-size:13px; font-weight:750; margin-bottom:8px;}
        .ingredient-chip {display:block; width:fit-content; min-width:180px; margin:7px 0; padding:8px 12px; border-radius:10px; background:#fff; border:1px solid #9ac8aa; color:#14532d; font-size:13px; font-weight:650; box-shadow:0 2px 6px rgba(20,83,45,.05);}
        div.stButton > button[aria-label$="×"] {background:#fff; border:1px solid #8fc7a2; color:#14532d; border-radius:999px; font-weight:700; min-height:38px;}
        div.stButton > button[aria-label$="×"]:hover {background:#fff1e8; border-color:#f0a36a; color:#9a3412;}
        .photo-analysis-head {padding:2px 0 10px;}
        .photo-analysis-head h3 {font-size:26px; font-weight:850; margin:0 0 8px; display:flex; align-items:center; gap:11px;}
        .photo-analysis-icon {display:inline-flex; align-items:center; justify-content:center; width:42px; height:42px; border-radius:12px; background:#fff0d5; color:#d97706; font-size:21px; box-shadow:0 4px 12px rgba(217,119,6,.12);}
        .photo-analysis-head p {margin:0; color:#526779; font-size:15px; font-weight:520; line-height:1.6;}
        .st-key-ingredient_upload_card,
        .st-key-label_upload_card,
        .st-key-meal_upload_card {margin:18px 0 20px; padding:27px 30px; border:2px solid #bfe4ca; border-radius:24px; background:linear-gradient(120deg,#ffffff 0%,#fbfefc 100%); box-shadow:0 12px 28px rgba(15,23,42,.08);}
        .upload-card-copy {display:flex; align-items:center; gap:22px; min-height:118px;}
        .upload-card-icon {display:flex; flex:0 0 106px; align-items:center; justify-content:center; width:106px; height:106px; border-radius:50%; background:radial-gradient(circle at 35% 30%,#f8fff9,#e4f5e8); color:#219349; font-size:52px;}
        .upload-card-copy h3 {margin:0 0 9px; color:#071a38; font-size:28px; line-height:1.15; font-weight:850;}
        .upload-card-copy p {margin:0 0 17px; color:#60708d; font-size:17px; line-height:1.5;}
        .upload-card-copy small {display:block; color:#42755a; font-size:14px; font-weight:700;}
        .st-key-ingredient_upload_card [data-testid="stFileUploaderDropzone"],
        .st-key-label_upload_card [data-testid="stFileUploaderDropzone"],
        .st-key-meal_upload_card [data-testid="stFileUploaderDropzone"] {display:flex; align-items:center; justify-content:center; min-height:118px !important; padding:0 !important; border:0 !important; background:transparent !important;}
        .st-key-ingredient_upload_card [data-testid="stFileUploaderDropzoneInstructions"],
        .st-key-label_upload_card [data-testid="stFileUploaderDropzoneInstructions"],
        .st-key-meal_upload_card [data-testid="stFileUploaderDropzoneInstructions"] {display:none !important;}
        .st-key-ingredient_upload_card [data-testid="stFileUploaderDropzone"] button,
        .st-key-label_upload_card [data-testid="stFileUploaderDropzone"] button,
        .st-key-meal_upload_card [data-testid="stFileUploaderDropzone"] button {width:100%; min-height:72px; padding:0 22px; border:0 !important; border-radius:16px; background:linear-gradient(135deg,#16803f,#24a556) !important; color:#fff !important; box-shadow:0 10px 18px rgba(31,122,67,.22); font-size:0 !important; font-weight:850;}
        .st-key-ingredient_upload_card [data-testid="stFileUploaderDropzone"] button *,
        .st-key-label_upload_card [data-testid="stFileUploaderDropzone"] button *,
        .st-key-meal_upload_card [data-testid="stFileUploaderDropzone"] button * {display:none !important;}
        .st-key-ingredient_upload_card [data-testid="stFileUploaderDropzone"] button::after,
        .st-key-label_upload_card [data-testid="stFileUploaderDropzone"] button::after,
        .st-key-meal_upload_card [data-testid="stFileUploaderDropzone"] button::after {content:"Fotoğraf Seç"; color:#fff; font-size:18px; font-weight:850;}
        /* Yükleme sonrasında oluşan dosya satırındaki kaldırma düğmesini,
           büyük yeşil "Fotoğraf Seç" düğmesi stilinden ayır. */
        .st-key-meal_upload_card [data-testid="stFileUploaderFile"] button {
            width:36px !important;
            min-width:36px !important;
            min-height:36px !important;
            padding:0 !important;
            border:0 !important;
            border-radius:50% !important;
            background:transparent !important;
            color:#526779 !important;
            box-shadow:none !important;
            font-size:inherit !important;
        }
        .st-key-meal_upload_card [data-testid="stFileUploaderFile"] button::after {content:none !important;}
        .st-key-meal_upload_card [data-testid="stFileUploaderFile"] button * {display:initial !important;}
        .discover-intro {margin:2px 0 24px; padding:20px 23px; border-left:6px solid #1f7a43; border-radius:15px; background:linear-gradient(120deg,#edf8f0,#fff8e8); box-shadow:0 7px 18px rgba(31,122,67,.08);}
        .discover-intro h1 {margin:0 0 7px; font-size:38px; line-height:1.15; font-weight:850;}
        .discover-intro p {margin:0; color:#425d4d; font-size:16px; line-height:1.6; font-weight:550;}
        .discover-intro + .muted {display:none;}
        .feature-section-intro {display:flex; align-items:center; gap:18px; margin:10px 0 24px; padding:20px 23px; border:1px solid #cfe8d7; border-left:5px solid #1f7a43; border-radius:16px; background:linear-gradient(120deg,#edf8f0,#fff8e8); box-shadow:0 7px 18px rgba(31,122,67,.08);}
        .feature-section-icon {display:flex; align-items:center; justify-content:center; flex:0 0 60px; width:60px; height:60px; border-radius:50%; background:#e5f5e9; color:#18743c; font-size:29px;}
        .feature-section-intro h1 {margin:0 0 7px; color:#071a38 !important; font-size:34px; line-height:1.15; font-weight:850;}
        .feature-section-intro p {margin:0; color:#425d4d; font-size:16px; line-height:1.6; font-weight:550;}
        div[data-testid="stVerticalBlock"]:has(.discover-intro) h3 {font-size:27px !important; font-weight:850 !important; margin-top:22px !important;}
        div[data-testid="stVerticalBlock"]:has(.discover-intro) h4 {font-size:23px !important; font-weight:820 !important; margin-top:18px !important;}
        div[data-testid="stVerticalBlock"]:has(.discover-intro) label,
        div[data-testid="stVerticalBlock"]:has(.discover-intro) [data-testid="stCaptionContainer"] {font-size:14px !important; color:#40574b !important; font-weight:600 !important;}
        .label-analysis-head {display:flex; align-items:center; gap:15px; margin:12px 0 20px; padding:20px 22px; border:1px solid #c5e4cf; border-radius:18px; background:linear-gradient(120deg,#edf8f0,#fff8e8);}
        .label-analysis-head h1 {margin:0 0 5px; font-size:29px;}
        .label-analysis-head p {margin:0; color:#64748b; font-size:13px; line-height:1.55;}
        .label-current-goal {width:fit-content; margin:0 0 18px; padding:10px 14px; border:1px solid #a8d5b5; border-radius:12px; background:#edf8f0; color:#28543a; font-size:13px;}
        .label-upload-empty {padding:22px; border:1px dashed #e2ad52; border-radius:15px; background:#fffaf0; color:#73511d; text-align:center; font-weight:650;}
        .label-result-hero {display:flex; align-items:center; justify-content:space-between; gap:24px; margin:28px 0 8px; padding:23px 25px; border:1px solid #a8d5b5; border-radius:18px; background:linear-gradient(125deg,#eaf7ee,#fff8e8); box-shadow:0 8px 22px rgba(31,122,67,.09);}
        .label-result-hero span {color:#1f7a43; font-size:11px; font-weight:850; letter-spacing:.08em;}
        .label-result-hero h2 {margin:5px 0 4px; font-size:27px;}
        .label-result-hero p {margin:0; color:#526b5c; font-size:13px;}
        .label-score {display:grid; grid-template-columns:auto auto; align-items:end; min-width:180px; padding:14px 18px; border:1px solid #f0c36b; border-radius:15px; background:#fff; color:#14532d; text-align:center;}
        .label-score strong {font-size:38px; line-height:1;}
        .label-score small {padding:0 0 4px 3px; color:#64748b;}
        .label-score em {grid-column:1 / -1; margin-top:5px; color:#7a5417; font-size:11px; font-style:normal; font-weight:750;}
        .label-reading-note {margin:12px 0; padding:10px 13px; border-radius:11px; background:#f4f8f5; color:#476156; font-size:12px; font-weight:650;}
        .label-disclaimer {margin:16px 0 8px; padding:14px 16px; border-left:4px solid #e59b2f; border-radius:10px; background:#fff7e6; color:#684a1c; font-size:12px; line-height:1.55;}
        [data-testid="stTabs"] [data-baseweb="tab-list"] {gap:9px; padding:7px 7px 0; border:1px solid #dce8df; border-radius:14px 14px 0 0; background:#f8fbf9; overflow-x:auto;}
        [data-testid="stTabs"] [data-baseweb="tab"] {height:62px; padding:0 24px; border:2px solid #d1e0d6; border-bottom:0; border-radius:13px 13px 0 0; background:#fff; color:#0b2347 !important; font-size:18px !important; font-weight:850 !important; white-space:nowrap;}
        [data-testid="stTabs"] [data-baseweb="tab"] p,
        [data-testid="stTabs"] [data-baseweb="tab"] span,
        [data-testid="stTabs"] [data-baseweb="tab"] div {color:#0b2347 !important; font-size:18px !important; font-weight:850 !important; letter-spacing:.05px;}
        [data-testid="stTabs"] [aria-selected="true"] {border-color:#178042; background:#eaf8ee; color:#075d32 !important; box-shadow:inset 0 -4px 0 #147a3c,0 4px 10px rgba(20,122,67,.12);}
        [data-testid="stTabs"] [aria-selected="true"] p,
        [data-testid="stTabs"] [aria-selected="true"] span,
        [data-testid="stTabs"] [aria-selected="true"] div {color:#075d32 !important;}
        [data-testid="stTabs"] [data-baseweb="tab-highlight"] {display:none;}
        .stTabs [role="tablist"], div[role="tablist"] {gap:10px !important; padding:8px 8px 0 !important; border:1px solid #dce8df !important; border-radius:15px 15px 0 0 !important; background:#f8fbf9 !important; overflow-x:auto !important;}
        .stTabs button[role="tab"], div[role="tablist"] button[role="tab"] {height:56px !important; min-height:56px !important; padding:0 22px !important; border:1.5px solid #d2dfd6 !important; border-bottom:0 !important; border-radius:12px 12px 0 0 !important; background:#fffdf8 !important; color:#183527 !important; font-size:17px !important; font-weight:800 !important; white-space:nowrap !important;}
        .stTabs button[role="tab"] p, div[role="tablist"] button[role="tab"] p {font-size:17px !important; line-height:1.2 !important; font-weight:800 !important; color:inherit !important;}
        button[data-baseweb="tab"],
        .stTabs [data-baseweb="tab"] {
            min-height:68px !important; padding:0 27px !important;
            color:#0b213d !important; font-size:21px !important; font-weight:850 !important;
            border-width:2px !important;
        }
        button[data-baseweb="tab"] p,
        .stTabs [data-baseweb="tab"] p,
        [data-baseweb="tab-list"] p {
            font-size:21px !important; line-height:1.25 !important;
            font-weight:850 !important; color:inherit !important; letter-spacing:.005em !important;
        }
        button[data-baseweb="tab"][aria-selected="true"],
        .stTabs [data-baseweb="tab"][aria-selected="true"] {
            color:#0d6332 !important; background:#e2f4e7 !important;
            border-color:#5ca675 !important; box-shadow:inset 0 -5px 0 #18733d !important;
        }
        .stTabs button[role="tab"][aria-selected="true"], div[role="tablist"] button[role="tab"][aria-selected="true"] {border-color:#62a878 !important; background:#e4f5e9 !important; color:#0f6332 !important; box-shadow:inset 0 -4px 0 #1f7a43,0 3px 9px rgba(31,122,67,.1) !important;}
        .meal-analysis-head {display:flex; align-items:center; gap:15px; margin:12px 0 20px; padding:20px 22px; border:1px solid #c5e4cf; border-radius:18px; background:linear-gradient(120deg,#edf8f0,#fff4df);}
        .meal-analysis-head > span {display:flex; align-items:center; justify-content:center; width:50px; height:50px; flex:0 0 50px; border-radius:15px; background:#1f7a43; color:#fff; font-size:24px; box-shadow:0 8px 18px rgba(31,122,67,.2);}
        .meal-analysis-head h1 {margin:0 0 5px; font-size:29px;}
        .meal-analysis-head p {margin:0; color:#64748b; font-size:13px;}
        .meal-upload-empty {margin:10px 0 14px; padding:22px; border:1px dashed #e2ad52; border-radius:15px; background:#fffaf0; color:#73511d; text-align:center; font-weight:650;}
        .meal-preview-frame {width:100%; max-width:480px; aspect-ratio:1 / 1; overflow:hidden; border:1px solid #d8e6dc; border-radius:17px; background:#eef5f0; box-shadow:0 7px 20px rgba(15,23,42,.08);}
        .meal-preview-frame img {display:block; width:100%; height:100%; object-fit:cover; object-position:center;}
        .meal-preview-caption {max-width:480px; padding:7px 4px 0; color:#64748b; font-size:11px; text-align:center;}
        .meal-result-head {display:flex; align-items:center; gap:14px; margin:28px 0 16px; padding:20px 22px; border:1px solid #a8d5b5; border-radius:17px; background:linear-gradient(120deg,#edf8f0,#fff9ed);}
        .meal-result-head > span {display:flex; align-items:center; justify-content:center; width:46px; height:46px; flex:0 0 46px; border-radius:14px; background:#fff; font-size:23px;}
        .meal-result-head small {color:#1f7a43; font-size:10px; font-weight:850; letter-spacing:.08em;}
        .meal-result-head h2 {margin:3px 0; font-size:25px;}
        .meal-result-head p {margin:0; color:#526b5c; font-size:12px;}
        .meal-item-card {height:100%; min-height:215px; margin-bottom:14px; padding:17px; border:1px solid #dce9df; border-radius:16px; background:#fff; box-shadow:0 5px 15px rgba(15,23,42,.05);}
        .meal-item-title {color:#071a38; font-size:17px; font-weight:800;}
        .meal-item-grams {margin:5px 0 13px; color:#1f7a43; font-size:12px; font-weight:700;}
        .meal-item-grid {display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px;}
        .meal-item-grid span {padding:8px 9px; border-radius:9px; background:#f5f8f6; color:#64748b; font-size:10px;}
        .meal-item-grid b {display:block; margin-top:3px; color:#173527; font-size:12px;}
        .meal-confidence {width:fit-content; margin:14px 0; padding:9px 13px; border:1px solid #a8d5b5; border-radius:11px; background:#edf8f0; color:#28543a; font-size:12px;}
        .meal-disclaimer {margin:16px 0; padding:14px 16px; border-left:4px solid #e59b2f; border-radius:10px; background:#fff7e6; color:#684a1c; font-size:12px; line-height:1.55;}
        .weekly-summary-note {margin:12px 0 16px; padding:12px 15px; border:1px solid #c9e5d1; border-radius:12px; background:#eef9f1; color:#23533a; font-size:14px;}
        .weekly-chart {margin:8px 0 25px; padding:18px 20px 16px; border:1px solid #d7e8dc; border-radius:16px; background:#fbfefc;}
        .weekly-chart h4 {margin:0 0 12px; color:#092344; font-size:18px;}
        .weekly-chart-bars {display:flex; align-items:flex-end; gap:12px; height:170px; padding:8px 5px 0; border-bottom:1px solid #dce8df;}
        .weekly-chart-bar {display:grid; grid-template-rows:20px 1fr 23px; flex:1; min-width:0; height:160px; justify-items:center; align-items:end;}
        .weekly-chart-bar span {color:#335a43; font-size:12px; font-weight:750;}
        .weekly-chart-bar i {display:block; align-self:end; width:min(52px,72%); min-height:5px; border-radius:9px 9px 2px 2px; background:linear-gradient(180deg,#49a76c,#1f7a43);}
        .weekly-chart-bar small {align-self:end; color:#526b5c; font-size:12px; white-space:nowrap;}
        .result-ingredients {display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin:14px 0 20px; padding:13px 15px; background:#f0f9f3; border:1px solid #c5e4cf; border-radius:14px;}
        .result-ingredients-title {color:#166534; font-size:13px; font-weight:750; margin-right:4px;}
        .result-ingredient-chip {display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; background:#fff; border:1px solid #9ac8aa; color:#14532d; font-size:12px; font-weight:650;}
        .result-goal-banner {display:flex; align-items:center; gap:12px; width:fit-content; min-width:390px; margin:12px 0 18px; padding:13px 17px; border:1px solid #9fd0ae; border-left:5px solid #1f7a43; border-radius:13px; background:linear-gradient(110deg,#eaf7ee,#fff8e9); box-shadow:0 5px 14px rgba(31,122,67,.08);}
        .result-goal-banner > span {display:flex; align-items:center; justify-content:center; width:34px; height:34px; flex:0 0 34px; border-radius:10px; background:#fff; font-size:18px;}
        .result-goal-banner strong {display:block; color:#14532d; font-size:15px; font-weight:800;}
        .result-goal-banner small {display:block; margin-top:3px; color:#526b5c; font-size:12px;}
        .workout-section-head {display:flex; align-items:center; gap:14px; padding:19px 21px; margin-bottom:16px; border:1px solid #c5e4cf; border-radius:17px; background:linear-gradient(120deg,#eef9f1 0%,#fffaf0 100%);}
        .workout-section-head > span {display:flex; align-items:center; justify-content:center; width:44px; height:44px; flex:0 0 44px; border-radius:13px; background:#1f7a43; color:#fff; font-size:17px; box-shadow:0 7px 16px rgba(31,122,67,.18);}
        .workout-section-head h2 {margin:0 0 3px; font-size:24px;}
        .workout-section-head p {margin:0; color:#64748b; font-size:13px;}
        .workout-current-goal {width:fit-content; margin:0 0 16px; padding:10px 14px; border:1px solid #a8d5b5; border-radius:12px; background:#edf8f0; color:#28543a; font-size:13px;}
        .workout-empty {margin-top:16px; padding:15px 17px; border:1px dashed #e6ba69; border-radius:14px; background:#fffaf0; color:#71511d; font-size:13px;}
        .workout-video-title {min-height:45px; margin:7px 0 5px; color:#071a38; font-size:15px; line-height:1.45; font-weight:800;}
        .workout-channel {margin-bottom:12px; color:#64748b; font-size:12px; font-weight:650;}
        .pagination-status {text-align:center; color:#64748b; padding:10px 0; font-size:13px;}
        .assistant-side-card {background:#fff; border:1px solid #e2e8e4; border-radius:17px; padding:17px; margin-bottom:14px; box-shadow:0 5px 15px rgba(15,23,42,.05);}
        .assistant-side-card h3 {font-size:18px; margin:0 0 13px;}
        .assistant-recipe-image {width:100%; height:165px; object-fit:cover; border-radius:13px; margin-bottom:12px;}
        .assistant-recipe-name {font-size:17px; font-weight:800; color:#071a38; margin-bottom:8px;}
        .assistant-mini-meta {color:#64748b; font-size:12px; line-height:1.7;}
        .assistant-list {display:flex; flex-direction:column; gap:8px;}
        .assistant-list span {display:flex; align-items:center; gap:8px; color:#334155; font-size:13px; padding-bottom:7px; border-bottom:1px dashed #e5e7eb;}
        .assistant-list span:last-child {border-bottom:0; padding-bottom:0;}
        .assistant-check {display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px; border-radius:50%; background:#e9f7ee; color:#1f7a43; font-size:12px; font-weight:800;}
        .assistant-goal {padding:13px; border-radius:13px; background:#e9f7ee; color:#166534; font-weight:750; text-align:center;}
        .assistant-header {display:flex; align-items:center; gap:13px; margin-bottom:6px;}
        .assistant-avatar {width:52px; height:52px; display:flex; align-items:center; justify-content:center; border-radius:50%; background:#e9f7ee; color:#1f7a43; font-size:27px; border:1px solid #c5e4cf;}
        .assistant-header h1 {font-size:29px; margin:0;}
        .assistant-online {color:#1f7a43; font-size:12px; font-weight:650; margin-top:4px;}
        .assistant-intro {margin:12px 0 16px; padding:13px 15px; border-radius:13px; background:#f3faf5; border:1px solid #d7eadc; color:#476156; font-size:13px;}
        .assistant-quick-label {font-size:12px; color:#64748b; font-weight:700; margin:15px 0 7px;}
        .inline-assistant-head {display:flex; align-items:center; gap:15px; margin:2px 0 10px; padding:4px 0;}
        .inline-assistant-head .assistant-avatar {width:52px; height:52px; font-size:25px; flex:0 0 52px; border:2px solid #a8d5b5; box-shadow:0 5px 13px rgba(31,122,67,.12);}
        .inline-assistant-head h3 {font-size:25px; line-height:1.2; font-weight:850; color:#071a38; margin:0;}
        .inline-assistant-head p {font-size:14px; line-height:1.5; font-weight:550; color:#526b5c; margin:5px 0 0;}
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) {
            border:2px solid #a8d5b5 !important; border-radius:19px !important;
            background:linear-gradient(135deg,#f2fbf5 0%,#fffaf1 100%) !important;
            box-shadow:0 9px 24px rgba(31,122,67,.10) !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) [data-testid="stAlert"] {
            border:1px solid #b9d8c2 !important; background:#e9f5ed !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) [data-testid="stAlert"] p {
            font-size:14px !important; font-weight:650 !important; color:#205d39 !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) div.stButton > button {
            min-height:52px !important; border-width:1.5px !important; font-size:16px !important; font-weight:780 !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) [data-testid="stTextInput"] input {
            min-height:66px !important; padding:15px 18px !important;
            font-size:18px !important; line-height:1.4 !important; font-weight:600 !important;
            color:#071a38 !important; background:#fff !important; border:2px solid #9db9a7 !important;
            caret-color:#1f7a43 !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) [data-testid="stTextInput"] input::placeholder {
            color:#64748b !important; opacity:1 !important; font-size:16px !important; font-weight:500 !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) [data-testid="stTextInput"] input:focus {
            border-color:#1f7a43 !important; box-shadow:0 0 0 3px rgba(31,122,67,.14) !important;
        }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.inline-assistant-head) [data-testid="stFormSubmitButton"] button {
            min-height:58px !important; font-size:17px !important; font-weight:850 !important;
        }
        [data-testid="stChatMessage"] {background:#f8faf9; border:1px solid #e5ebe7; border-radius:15px; padding:10px 14px; margin:8px 0;}
        [data-testid="stChatMessage"] p {line-height:1.55;}
        div.stButton > button {border-radius:999px; font-weight:650; min-height:39px;}
        div.stButton > button[kind="primary"] {background:#1f7a43; border-color:#1f7a43;}
        [class*="st-key-nav_"] button, .st-key-nav_discover button {
            min-height:46px !important; padding:8px 18px !important; font-size:16px !important; font-weight:800 !important;
            color:#071a38 !important; border:2px solid #c8cec9 !important; background:#fffdfa !important;
            box-shadow:0 3px 8px rgba(15,23,42,.06) !important;
        }
        [class*="st-key-nav_"] button p, .st-key-nav_discover button p {
            color:inherit !important; font-size:16px !important; font-weight:800 !important; letter-spacing:.01em !important;
        }
        [class*="st-key-nav_"] button[kind="primary"], .st-key-nav_discover button[kind="primary"] {
            color:#fff !important; background:#1f7a43 !important; border-color:#1f7a43 !important;
            box-shadow:0 6px 14px rgba(31,122,67,.18) !important;
        }
        .st-key-hero_browse button {
            min-height:48px !important; padding:10px 20px !important; font-size:15px !important; font-weight:800 !important;
            box-shadow:0 8px 18px rgba(31,122,67,.18) !important;
        }
        div.stButton > button[aria-label="Ana Sayfa"],
        div.stButton > button[aria-label="Tarifler"],
        div.stButton > button[aria-label="Hakkımızda"] {
            min-height:44px; border-radius:12px; font-size:15px; font-weight:750;
            padding:8px 15px; box-shadow:0 2px 7px rgba(15,23,42,.05);
        }
        div.stButton > button[aria-label="Ana Sayfa"][kind="secondary"],
        div.stButton > button[aria-label="Tarifler"][kind="secondary"],
        div.stButton > button[aria-label="Hakkımızda"][kind="secondary"] {
            background:#fff; border-color:#d8e1da; color:#10233f;
        }
        div.stButton > button[aria-label="Ana Sayfa"]:hover,
        div.stButton > button[aria-label="Tarifler"]:hover,
        div.stButton > button[aria-label="Hakkımızda"]:hover {color:#1f7a43; border-color:#b8ddc4; background:#f3faf5;}
        .active-nav-line {display:none;}
        div.stButton > button[aria-label*="Kilo Verme"],
        div.stButton > button[aria-label*="Dengeli Beslenme"],
        div.stButton > button[aria-label*="Kas Yapma"] {
            border-radius:16px; min-height:84px; text-align:left; padding:16px 20px;
            background:#f5f1e8; border-color:#ded8ca; color:#10233f;
            white-space:pre-line; line-height:1.5;
        }
        div[data-baseweb="input"] {border-radius:12px;}
        div.stTextInput div[data-baseweb="input"] {
            min-height:64px !important; border:2px solid #9db9a7 !important;
            border-radius:13px !important; background:#fff !important;
        }
        div.stTextInput div[data-baseweb="input"]:focus-within {
            border-color:#1f7a43 !important; box-shadow:0 0 0 3px rgba(31,122,67,.14) !important;
        }
        div.stTextInput input {
            min-height:60px !important; padding:14px 17px !important;
            color:#071a38 !important; font-size:19px !important; line-height:1.4 !important;
            font-weight:650 !important; caret-color:#1f7a43 !important;
        }
        div.stTextInput input::placeholder {
            color:#64748b !important; opacity:1 !important; font-size:17px !important; font-weight:500 !important;
        }
        /* Beslenme hedeflerini Figma'daki secim kartlarina yaklastir */
        div[data-testid="stRadio"] > div[role="radiogroup"] {gap:12px;}
        div[data-testid="stRadio"] > div[role="radiogroup"] > label {
            background:#fff; border:1px solid #e5e7eb; border-radius:16px;
            padding:19px 50px 19px 21px; min-height:86px; align-items:center;
            position:relative;
            transition:all .15s ease;
        }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:hover {
            border-color:#9ac8aa; background:#fbfefc;
        }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) {
            border-color:#1f7a43; background:#e9f7ee;
            box-shadow:0 0 0 1px #1f7a43;
        }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label > div:first-child {display:none;}
        div[data-testid="stRadio"] > div[role="radiogroup"] > label p {
            color:#10233f; font-size:15px; font-weight:680; line-height:1.55; white-space:pre-line;
            margin:0;
        }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label p::first-line {font-size:17px; font-weight:820;}
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked)::after {
            content:"✓"; position:absolute; right:18px; top:50%; transform:translateY(-50%);
            width:20px; height:20px; border-radius:50%; background:#1f7a43; color:white;
            font-size:13px; font-weight:800; display:flex; align-items:center; justify-content:center;
        }
        .st-key-ingredient_form_input input {min-height:50px !important; font-size:15px !important;}
        [class*="st-key-quick_"] button {min-height:47px !important; font-size:14px !important; font-weight:750 !important;}
        .st-key-ingredient_form button {min-height:48px !important; font-size:14px !important; font-weight:800 !important;}
        .st-key-discover_recipes_submit button {
            color:#ffffff !important;
            font-size:17px !important;
            font-weight:850 !important;
            text-shadow:0 1px 1px rgba(0,0,0,.12) !important;
        }
        .st-key-discover_recipes_submit button p,
        .st-key-discover_recipes_submit button span,
        .st-key-discover_recipes_submit button div {
            color:#ffffff !important;
            font-size:17px !important;
            font-weight:850 !important;
        }
        .st-key-ingredient_photo_uploader [data-testid="stFileUploaderDropzone"] {min-height:92px; background:#fbfdfb; border:1.5px dashed #9ac8aa;}
        /* Tarifimi Kesfet ekranindaki dort ana sekmeyi belirginlestir. */
        [data-testid="stTabs"] > div > [data-baseweb="tab-list"] {
            gap:12px !important; padding:10px 10px 0 !important;
            min-height:76px !important; align-items:flex-end !important;
            border:2px solid #d7e8dc !important; background:#f6faf7 !important;
        }
        [data-testid="stTabs"] button[role="tab"] {
            min-height:70px !important; height:70px !important;
            padding:0 28px !important; border:2px solid #d4e1d8 !important;
            border-bottom:0 !important; border-radius:13px 13px 0 0 !important;
            background:#fff !important; color:#10233f !important;
            font-size:22px !important; font-weight:900 !important;
        }
        [data-testid="stTabs"] button[role="tab"] p {
            margin:0 !important; color:inherit !important;
            font-size:22px !important; line-height:1.2 !important; font-weight:900 !important;
        }
        [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
            color:#0c6634 !important; border-color:#62a878 !important;
            background:#e4f5e9 !important;
            box-shadow:inset 0 -5px 0 #1f7a43,0 4px 12px rgba(31,122,67,.12) !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] {
            display:grid !important; grid-template-columns:1.35fr 1.22fr 1.22fr .88fr 1.42fr !important;
            gap:12px !important; padding:10px !important; border:2px solid #d7e8dc !important;
            border-radius:16px !important; background:#f6faf7 !important;
        }
        /* st.radio'nun boş widget etiketi bir seçenek değildir; kart görünümü
           yalnızca radio grubundaki gerçek seçenek etiketlerine uygulanır. */
        .st-key-discover_section_nav [data-testid="stRadio"] > label,
        .st-key-discover_section_nav [data-testid="stRadio"] > div > label {
            display:none !important; height:0 !important; min-height:0 !important;
            margin:0 !important; padding:0 !important; border:0 !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label {
            display:flex !important; align-items:center !important; justify-content:center !important; min-height:62px !important;
            min-width:0 !important; overflow:hidden !important; padding:9px 10px !important; border:2px solid #d3e0d7 !important;
            border-radius:13px !important; background:#fff !important; text-align:center !important;
            box-shadow:0 3px 9px rgba(15,23,42,.05) !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label [data-baseweb="radio"],
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label input,
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked)::after {display:none !important;}
        .st-key-discover_section_nav [data-testid="stRadio"] [data-baseweb="radio"],
        .st-key-discover_section_nav [data-testid="stRadio"] input[type="radio"] {display:none !important;}
        .st-key-discover_section_nav div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked)::after {
            content:none !important; display:none !important;
        }
        .st-key-discover_section_nav {padding:10px !important; border:2px solid #d7e8dc !important; border-radius:16px !important; background:#f6faf7 !important;}
        .st-key-discover_section_nav [data-testid="stWidgetLabel"] {display:none !important;}
        .discover-section-title {margin:6px 0 14px; color:#071a38; font-size:25px; font-weight:850; line-height:1.2; text-align:center;}
        .discover-section-title::after {content:""; display:block; width:52px; height:3px; margin:10px auto 0; border-radius:99px; background:#1f7a43;}
        .st-key-discover_section_nav [data-testid="stHorizontalBlock"] {gap:12px !important;}
        .st-key-discover_section_nav div.stButton > button {
            min-height:64px !important; padding:10px 10px !important; border:2px solid #bfd5c7 !important;
            border-radius:13px !important; background:#fff !important; color:#10233f !important;
            font-size:16px !important; line-height:1.2 !important; font-weight:800 !important;
            white-space:nowrap !important; box-shadow:0 3px 9px rgba(15,23,42,.05) !important;
        }
        .st-key-discover_section_nav div.stButton > button[kind="primary"] {
            border-color:#1f7a43 !important; background:#e4f5e9 !important; color:#0c6634 !important;
            box-shadow:inset 0 -5px 0 #1f7a43,0 5px 12px rgba(31,122,67,.13) !important;
        }
        .st-key-discover_section_nav div.stButton > button p,
        .st-key-discover_section_nav div.stButton > button span,
        .st-key-discover_section_nav div.stButton > button div {
            color:inherit !important; font-size:16px !important; line-height:1.2 !important;
            font-weight:800 !important;
        }
        .st-key-discover_section_button_0 button,
        .st-key-discover_section_button_1 button,
        .st-key-discover_section_button_2 button,
        .st-key-discover_section_button_3 button,
        .st-key-discover_section_button_4 button {
            min-height:64px !important; padding:10px 10px !important;
            border:2px solid #bcd4c5 !important; border-radius:15px !important;
            color:#061b3d !important; background:#fff !important;
            font-size:16px !important; font-weight:800 !important; line-height:1.2 !important;
            white-space:nowrap !important;
        }
        .st-key-discover_section_button_0 button *,
        .st-key-discover_section_button_1 button *,
        .st-key-discover_section_button_2 button *,
        .st-key-discover_section_button_3 button *,
        .st-key-discover_section_button_4 button * {
            color:inherit !important; font-size:16px !important;
            font-weight:800 !important; line-height:1.2 !important;
        }
        /* Streamlit bazı sürümlerde üst kapsayıcı sınıfını DOM'a taşımaz.
           Sekme düğmelerinin kendi key sınıflarını hedeflemek daha kararlıdır. */
        div[class*="st-key-discover_tab_"] button {
            min-height:64px !important; padding:10px 10px !important;
            color:#071a38 !important; font-size:16px !important;
            font-weight:800 !important; line-height:1.2 !important;
            white-space:nowrap !important;
        }
        div[class*="st-key-discover_tab_"] button p,
        div[class*="st-key-discover_tab_"] button span,
        div[class*="st-key-discover_tab_"] button div {
            color:inherit !important; font-size:16px !important;
            font-weight:800 !important; line-height:1.2 !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label p,
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label span,
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label div:not([data-baseweb="radio"]) {
            color:#10233f !important; font-size:14px !important; line-height:1.18 !important;
            font-weight:800 !important; white-space:normal !important; overflow-wrap:break-word !important;
            overflow:hidden !important; text-overflow:clip !important; margin:0 !important; padding:0 !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) {
            border-color:#1f7a43 !important; background:#e4f5e9 !important;
            box-shadow:inset 0 -5px 0 #1f7a43,0 5px 12px rgba(31,122,67,.13) !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) p {color:#0c6634 !important;}
        @media(max-width:1024px){
            .block-container,[data-testid="stMainBlockContainer"]{width:94% !important; padding:20px 20px 56px !important;}
            [data-testid="stHorizontalBlock"]:has(.home-hero-copy){
                flex-direction:column !important; height:auto; min-height:auto; gap:0; padding:0; margin-bottom:36px;
            }
            [data-testid="stHorizontalBlock"]:has(.home-hero-copy) > [data-testid="stColumn"]{
                width:100% !important; flex:1 1 100% !important;
            }
            [data-testid="stHorizontalBlock"]:has(.home-hero-copy) > [data-testid="stColumn"]:last-child{min-height:360px; height:360px;}
            [data-testid="stHorizontalBlock"]:has(.home-step-card),
            [data-testid="stHorizontalBlock"]:has(.recipe-card){flex-wrap:wrap !important;}
            [data-testid="stHorizontalBlock"]:has(.home-step-card) > [data-testid="stColumn"],
            [data-testid="stHorizontalBlock"]:has(.recipe-card) > [data-testid="stColumn"]{
                min-width:calc(50% - 12px) !important; flex:1 1 calc(50% - 12px) !important;
            }
            .hero-title{font-size:46px;}
            .hero-image,.hero-image-frame{height:360px; min-height:360px;}
            [data-testid="stHorizontalBlock"]:has(.home-hero-copy) > [data-testid="stColumn"]:first-child{padding:32px;}
            .home-recipe-card{display:block; height:auto; min-height:390px;}
            .home-recipe-card img,.home-recipe-card .recipe-image-placeholder{height:220px; min-height:220px;}
            .recipe-card img{height:220px;}
            [data-testid="stTabs"] button[role="tab"],
            [data-testid="stTabs"] button[role="tab"] p{font-size:21px !important; font-weight:900 !important;}
            [data-testid="stTabs"] button[role="tab"]{min-height:68px !important; height:68px !important; padding:0 22px !important;}
            .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"]{grid-template-columns:repeat(2,minmax(0,1fr)) !important;}
        }
        @media(max-width:700px){
            .hero-title{font-size:34px; letter-spacing:-.5px;}
            .block-container,[data-testid="stMainBlockContainer"]{width:100% !important; padding:16px 16px 48px !important;}
            [data-testid="stHorizontalBlock"]:has(.home-hero-copy){padding:24px 18px; border-radius:18px;}
            [data-testid="stHorizontalBlock"]:has(.home-step-card) > [data-testid="stColumn"],
            [data-testid="stHorizontalBlock"]:has(.recipe-card) > [data-testid="stColumn"]{
                min-width:100% !important; flex:1 1 100% !important;
            }
            .hero-image,.hero-image-frame{height:280px;}
            .popular-heading h2{font-size:26px; gap:10px;}
            .popular-heading h2::before,.popular-heading h2::after{width:28px;}
            .step-content{gap:12px;}
            .recipe-card img{height:180px;}
            .hero-proof{margin-top:16px;}
            .home-steps-heading h2{font-size:27px;}
            [data-testid="stTabs"] button[role="tab"],
            [data-testid="stTabs"] button[role="tab"] p{font-size:17px !important;}
            [data-testid="stTabs"] button[role="tab"]{min-height:58px !important; height:58px !important; padding:0 16px !important;}
            .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"]{
                display:flex !important; grid-template-columns:none !important; overflow-x:auto !important;
                flex-wrap:nowrap !important; scroll-snap-type:x proximity; scrollbar-width:thin;
            }
            .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label{
                flex:0 0 220px !important; scroll-snap-align:start;
            }
            .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"] > label p{font-size:17px !important;}
            [data-testid="stHorizontalBlock"]:has(.brand){flex-wrap:wrap !important; gap:8px !important;}
            [data-testid="stHorizontalBlock"]:has(.brand) > [data-testid="stColumn"]:first-child{
                min-width:100% !important; flex:1 1 100% !important;
            }
            [data-testid="stHorizontalBlock"]:has(.brand) > [data-testid="stColumn"]:not(:first-child){
                min-width:0 !important; flex:1 1 calc(50% - 8px) !important;
            }
            .st-key-navbar_profile [data-testid="stPopover"] > button{justify-content:center !important;}
        }
        .about-page {max-width:1240px; margin:0 auto; padding:10px 8px 24px; color:#10233f;}
        .about-top {display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:56px; align-items:center; padding:18px 0 30px;}
        .about-kicker {margin:0 0 14px; color:#1f7a43; font-size:12px; letter-spacing:1.7px; font-weight:850;}
        .about-kicker::after {content:""; display:block; width:42px; height:2px; margin-top:13px; background:#f5a313;}
        .about-title {margin:0 0 20px; max-width:540px; color:#071a38; font-size:42px; line-height:1.13; letter-spacing:-.7px; font-weight:800;}
        .about-copy {max-width:535px; margin:0 0 15px; color:#304764; font-size:16px; line-height:1.75;}
        .about-image {display:block; width:100%; height:330px; object-fit:cover; border-radius:20px;}
        .about-quote {display:flex; align-items:center; justify-content:center; gap:24px; margin:0 -8px; padding:24px 32px; background:#edf7ef; color:#215e43; font-size:21px; font-weight:750; line-height:1.4; text-align:center;}
        .about-values {display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); margin:34px 0 28px;}
        .about-value {min-height:146px; padding:0 36px; text-align:center; border-right:1px solid #d8e2da;}
        .about-value:last-child {border-right:0;}
        .about-value-icon {display:block; height:34px; color:#1f7a43; font-size:31px; line-height:34px;}
        .about-value h3 {position:relative; margin:13px 0 17px; color:#10233f !important; font-size:19px; font-weight:800;}
        .about-value h3::after {content:""; position:absolute; width:28px; height:2px; left:50%; bottom:-10px; transform:translateX(-50%); background:#f5a313;}
        .about-value p {margin:0; color:#40536b; font-size:14px; line-height:1.55;}
        .about-note {display:flex; align-items:center; gap:10px; padding:17px 0 0; border-top:1px solid #dbe4dc; color:#506077; font-size:13px;}
        .about-note span {color:#1f7a43; font-size:18px;}
        @media(max-width:800px) {
            .about-top {grid-template-columns:1fr; gap:26px; padding-top:4px;}
            .about-title {font-size:34px;}
            .about-image {height:260px;}
            .about-quote {padding:22px 20px; font-size:17px;}
            .about-values {grid-template-columns:1fr; gap:24px;}
            .about-value {min-height:0; padding:0 14px 24px; border-right:0; border-bottom:1px solid #d8e2da;}
            .about-value:last-child {border-bottom:0;}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_navigation(current: str, navigate: Callable[[str], None]) -> None:
    columns = st.columns([2.05, .9, .9, 1, 1.8, 1.55], gap="small")
    with columns[0]:
        st.markdown('''<div class="brand" translate="no">
            <span class="brand-mark" aria-hidden="true">
                <svg viewBox="0 0 52 46" role="img">
                    <path d="M4 5c12 0 21 5 25 15v21C17 39 7 30 4 18Z" fill="#08783f"/>
                    <path d="M48 8c-11 0-18 4-22 13v20c11-2 19-10 22-22Z" fill="#35a853"/>
                    <path d="M13 13c7 5 11 11 14 22M40 15c-7 5-11 11-14 22" fill="none" stroke="#fbfcfa" stroke-width="2.5" stroke-linecap="round"/>
                    <path d="m14 29 1.7 4.3L20 35l-4.3 1.7L14 41l-1.7-4.3L8 35l4.3-1.7Z" fill="#f5a313"/>
                </svg>
            </span>
            <span><span class="brand-nutri">Nutri</span><span class="brand-match">Match</span></span>
        </div>''', unsafe_allow_html=True)
    for column, label in zip(columns[1:4], ["Ana Sayfa", "Tarifler", "Hakkımızda"]):
        with column:
            button_type = "primary" if current == label else "secondary"
            if st.button(label, key=f"nav_{label}", type=button_type, use_container_width=True):
                navigate(label)
                st.rerun()

    with columns[4]:
        if st.button("Asistanımı Aç →", key="nav_discover", type="primary", use_container_width=True):
            navigate("Tarifimi Keşfet")
            st.rerun()

    # Google OIDC'den gelen görünen ad navbarın sağındaki tek profil menüsünde kullanılır.
    display_name = str(st.user.get("name") or "Kullanıcı") if st.user.is_logged_in else "Kullanıcı"
    with columns[5]:
        with st.container(key="navbar_profile"):
            with st.popover(f"👤 {display_name}", use_container_width=True):
                st.markdown(f'<div class="profile-menu-name">{escape(display_name)}</div>', unsafe_allow_html=True)
                if st.button("Çıkış Yap", key="oidc_logout", use_container_width=True):
                    st.logout()

    st.markdown('<div class="nav-divider"></div>', unsafe_allow_html=True)


def render_home(navigate: Callable[[str], None], popular_recipes: list[dict] | None = None) -> None:
    st.markdown(
        f'''<style>
        [data-testid="stHorizontalBlock"]:has(.home-hero-copy) {{
            background-image:linear-gradient(90deg,rgba(0,51,34,.14) 0%,rgba(0,51,34,.08) 44%,rgba(0,0,0,0) 66%),url("{HERO_IMAGE}") !important;
            background-position:center right !important;
            background-size:cover !important;
        }}
        </style>''',
        unsafe_allow_html=True,
    )
    left, right = st.columns([0.46, 0.54], gap="large", vertical_alignment="center")
    with left:
        st.markdown('<span class="home-hero-copy" aria-hidden="true">hero</span>', unsafe_allow_html=True)
        st.markdown('<div class="eyebrow">👤&nbsp; Kişisel Beslenme Asistanın</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="hero-title">Beslenmeni <span>keşfet,</span><br>'
            'tabağını <span>tanı,</span> hedefini <span>takip et</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p class="hero-description">Malzemelerinden tarif bul, öğünlerini analiz et ve beslenme hedefini tek bir yerde takip et.</p>',
            unsafe_allow_html=True,
        )
        if st.button("Hemen Keşfet  →", key="hero_browse", type="primary"):
            navigate("Tarifimi Keşfet")
            st.rerun()

    with right:
        st.markdown('<span class="home-hero-visual" aria-hidden="true"></span>', unsafe_allow_html=True)

    cards = [
        ("🥗", "Malzemelerimden Tarif Bul", "Elindeki malzemelerle sana özel tarifler bul."),
        ("🏷️", "Besin Etiketi Analizi", "Ürün etiketini tara, içeriğini anında analiz et."),
        ("🍽️", "Tabağımı Analiz Et", "Yemeğinin fotoğrafını yükle, besin değerlerini öğren."),
        ("▶", "Hedefime Uygun Videolar", "Hedeflerine uygun eğitim ve beslenme videolarını izle."),
        ("🗓️", "Beslenme Günlüğüm", "Günlük kalori ve makro hedeflerine ulaşmanı izle."),
    ]
    # API'nin önceki oturumda önbelleğe aldığı sonuçlar da burada tekrar
    # kontrol edilir. Böylece "öne çıkan sağlıklı" alanında tatlı, kanat veya
    # yoğun tereyağlı tarifler görünmez; yeterli uygun API sonucu yoksa seçili
    # yerel vitrin tarifleriyle tamamlanır.
    rejected_terms = ("cannoli", "dondurma", "ice cream", "çikolata", "chocolate", "kanat", "wing", "fried", "kızarmış", "tereyağı", "butter")
    recipes: list[dict] = []
    seen_recipe_names: set[str] = set()
    for recipe in popular_recipes or []:
        recipe_name = str(recipe.get("name", "")).strip()
        recipe_key = recipe_name.casefold()
        if (
            not recipe_name
            or any(term in recipe_key for term in rejected_terms)
            or float(recipe.get("calories", 0) or 0) > 550
            or float(recipe.get("protein", 0) or 0) < 12
        ):
            continue
        recipes.append(recipe)
        seen_recipe_names.add(recipe_key)
        if len(recipes) == 3:
            break
    for recipe in FEATURED_RECIPES:
        recipe_key = str(recipe.get("name", "")).strip().casefold()
        if len(recipes) == 3:
            break
        if recipe_key and recipe_key not in seen_recipe_names:
            recipes.append(recipe)
            seen_recipe_names.add(recipe_key)

    st.markdown('<div class="home-feature-heading"><h2>NutriMatch ile Neler Yapabilirsin?</h2></div>', unsafe_allow_html=True)
    top_columns = st.columns(3, gap="medium")
    for column, (icon, title, text) in zip(top_columns, cards[:3]):
        with column:
            st.markdown(
                f'<div class="home-feature-card"><div class="feature-icon">{icon}</div><h4>{title}</h4><p>{text}</p></div>',
                unsafe_allow_html=True,
            )
    bottom_columns = st.columns([1, 1.35, 1.35, 1], gap="medium")
    for column, (icon, title, text) in zip((bottom_columns[1], bottom_columns[2]), cards[3:]):
        with column:
            st.markdown(
                f'<div class="home-feature-card home-feature-card--wide"><div class="feature-icon">{icon}</div>'
                f'<div><h4>{title}</h4><p>{text}</p></div></div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div class="section popular-heading"><h2>Öne Çıkan Sağlıklı Tarifler</h2></div>', unsafe_allow_html=True)
    _recipe_grid(recipes, "Dengeli Beslenme", navigate, show_score=False, card_variant="home")


def _format_label_value(value: object, unit: str) -> str:
    if value is None:
        return "Okunamadı"
    number = float(value)
    formatted = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{formatted} {unit}".strip()


def _render_label_analysis_result(result: dict, goal: str) -> None:
    product_name = escape(str(result.get("product_name") or "Ürün adı okunamadı"))
    basis_type = escape(str(result.get("basis_type") or "bilinmiyor"))
    st.markdown(
        f'<div class="label-result-hero"><div><span>BESİN ETİKETİ RAPORU</span><h2>{product_name}</h2><p>Hedef: <b>{escape(goal)}</b> · Değerlerin ölçüsü: <b>{basis_type}</b></p></div></div>',
        unsafe_allow_html=True,
    )

    nutrient_items = [
        ("Enerji", result.get("energy_kj"), "kJ"),
        ("Kalori", result.get("calories_kcal"), "kcal"),
        ("Protein", result.get("protein_g"), "g"),
        ("Karbonhidrat", result.get("carbohydrates_g"), "g"),
        ("Şeker", result.get("sugar_g"), "g"),
        ("Yağ", result.get("fat_g"), "g"),
        ("Doymuş yağ", result.get("saturated_fat_g"), "g"),
        ("Lif", result.get("fiber_g"), "g"),
        ("Tuz", result.get("salt_g"), "g"),
        ("Sodyum", result.get("sodium_mg"), "mg"),
    ]
    st.markdown("### Okunan Besin Değerleri")
    for start in range(0, len(nutrient_items), 5):
        columns = st.columns(5, gap="small")
        for column, (label, value, unit) in zip(columns, nutrient_items[start:start + 5]):
            column.metric(label, _format_label_value(value, unit))

    serving_size = result.get("serving_size")
    summary_bits = []
    if serving_size is not None:
        summary_bits.append(f"Porsiyon miktarı: {_format_label_value(serving_size, 'g/ml')}")
    if summary_bits:
        st.markdown('<div class="label-reading-note">' + " · ".join(summary_bits) + "</div>", unsafe_allow_html=True)
    if result.get("detected_text_summary"):
        st.caption(str(result["detected_text_summary"]))

    positive_column, attention_column = st.columns(2, gap="medium")
    with positive_column:
        with st.container(border=True):
            st.markdown("### ✅ Olumlu Yönleri")
            points = result.get("positive_points") or ["Etiketten güvenilir bir olumlu nokta okunamadı."]
            for point in points:
                st.markdown(f"- {point}")
    with attention_column:
        with st.container(border=True):
            st.markdown("### 🟠 Dikkat Edilebilecek Noktalar")
            points = result.get("attention_points") or ["Etikette değerlendirilebilecek yeterli bilgi bulunamadı."]
            for point in points:
                st.markdown(f"- {point}")

    with st.container(border=True):
        st.markdown("### 🎯 Hedefine Göre Yorum")
        st.write(result.get("goal_explanation") or "Bu fotoğraftan hedefe göre güvenilir bir yorum üretilemedi.")

    unreadable = result.get("unreadable_fields") or []
    if unreadable:
        st.warning("Etikette okunamayan bilgiler: " + ", ".join(str(item) for item in unreadable))
    st.info("Fotoğraftan okunan değerleri ürün ambalajındaki orijinal besin tablosuyla mutlaka karşılaştır.")
    st.markdown('<div class="label-disclaimer">Bu analiz genel bilgilendirme amaçlıdır. Yapay zekâ etiketi hatalı okuyabilir; değerleri ürün ambalajından doğrulayın. Bu sonuç tıbbi veya diyetetik tavsiye değildir.</div>', unsafe_allow_html=True)


def _render_nutrition_label_analysis() -> None:
    st.markdown('<div class="feature-section-intro"><span class="feature-section-icon">🏷️</span><div><h1>Besin Etiketi Analizi</h1><p>Ürünün besin değerleri tablosunun net bir fotoğrafını yükle; yapay zekâ etiketi okuyup seçtiğin hedefe göre değerlendirsin.</p></div></div>', unsafe_allow_html=True)
    valid_goals = ["Kilo Verme", "Dengeli Beslenme", "Kas Yapma"]
    current_goal = st.session_state.get("goal")
    if current_goal not in valid_goals:
        current_goal = "Dengeli Beslenme"
        st.session_state.goal = current_goal
    if "nutrition_label_goal_selector" not in st.session_state:
        st.session_state.nutrition_label_goal_selector = current_goal

    def sync_label_goal() -> None:
        selected = st.session_state.nutrition_label_goal_selector
        st.session_state.goal = selected
        st.session_state.goal_selector = selected
        st.session_state.meal_analysis_goal_selector = selected
        st.session_state.video_goal_selector = selected

    st.markdown("#### Analiz hedefini seç")
    current_goal = st.radio(
        "Beslenme hedefin",
        valid_goals,
        horizontal=True,
        format_func=lambda option: f"{GOAL_ICONS[option]} {option}",
        key="nutrition_label_goal_selector",
        on_change=sync_label_goal,
        label_visibility="collapsed",
    )
    st.session_state.goal = current_goal
    st.markdown(f'<div class="label-current-goal">🎯 Analiz hedefin: <strong>{escape(current_goal)}</strong></div>', unsafe_allow_html=True)

    if "nutrition_label_cache" not in st.session_state:
        st.session_state.nutrition_label_cache = {}
    with st.container(key="label_upload_card"):
        upload_copy, upload_action = st.columns([2.2, 1], gap="large", vertical_alignment="center")
        with upload_copy:
            st.markdown(
                '<div class="upload-card-copy"><div class="upload-card-icon">🏷️</div><div>'
                '<h3>Besin etiketi fotoğrafını yükle</h3>'
                '<p>Ürünün besin değerleri tablosunun net ve okunaklı fotoğrafını seç.</p>'
                '<small>🛡 Fotoğrafın yalnızca analiz için kullanılır.</small></div></div>',
                unsafe_allow_html=True,
            )
        with upload_action:
            uploaded_label = st.file_uploader(
                "Fotoğraf Seç",
                type=["jpg", "jpeg", "png"],
                key="nutrition_label_uploader",
                help="En fazla 10 MB boyutunda, yazıları net görünen bir fotoğraf seç.",
                label_visibility="collapsed",
            )
    if uploaded_label is None:
        return

    image_bytes = uploaded_label.getvalue()
    if len(image_bytes) > 10 * 1024 * 1024:
        st.warning("Fotoğraf en fazla 10 MB olabilir.")
        return
    photo_hash = hashlib.sha256(image_bytes).hexdigest()
    if st.session_state.get("nutrition_label_photo_hash") != photo_hash:
        st.session_state.nutrition_label_photo_hash = photo_hash
        st.session_state.pop("nutrition_label_active_result", None)
        st.session_state.pop("nutrition_label_error", None)

    preview_column, action_column = st.columns([1.45, 1], gap="large")
    with preview_column:
        st.image(image_bytes, caption="Yüklenen besin etiketi", use_container_width=True)
    with action_column:
        st.markdown("#### Fotoğraf analize hazır")
        st.caption("Gemini isteği yalnızca aşağıdaki düğmeye bastığında bir kez gönderilir.")
        analyze_clicked = st.button("Etiketi Analiz Et", type="primary", key="analyze_nutrition_label", use_container_width=True)

    cache_key = f"{photo_hash}:{current_goal}"
    if analyze_clicked:
        st.session_state.pop("nutrition_label_error", None)
        try:
            with st.spinner("Etiket küçültülüyor, okunuyor ve hedefin için değerlendiriliyor..."):
                result = get_or_analyze_label(
                    st.session_state.nutrition_label_cache,
                    photo_hash,
                    current_goal,
                    lambda: analyze_nutrition_label(image_bytes, uploaded_label.type, current_goal),
                )
            st.session_state.nutrition_label_active_result = {"key": cache_key, "data": result}
        except NutritionLabelError as exc:
            st.session_state.pop("nutrition_label_active_result", None)
            st.session_state.nutrition_label_error = str(exc)

    if st.session_state.get("nutrition_label_error"):
        st.error(st.session_state.nutrition_label_error)
    active = st.session_state.get("nutrition_label_active_result", {})
    if active.get("key") == cache_key and isinstance(active.get("data"), dict):
        _render_label_analysis_result(active["data"], current_goal)


def _meal_value(value: object, unit: str = "") -> str:
    if value is None:
        return "—"
    number = float(value)
    formatted = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{formatted} {unit}".strip()


def _render_meal_result(result: dict, goal: str, photo_hash: str) -> None:
    if not result.get("is_meal_image"):
        st.warning("Bu görselde analiz edilebilecek bir tabak veya öğün bulunamadı. Yemeğin net göründüğü başka bir fotoğraf yükle.")
        return
    items = result.get("items") or []
    if not items:
        st.warning("Fotoğraf çok bulanık olabilir veya ürünler ayırt edilemedi. Daha aydınlık ve yakın bir fotoğraf yükle.")
        return

    meal_name = escape(str(result.get("meal_name") or "Analiz edilen öğün"))
    st.markdown(f'<div class="meal-result-head"><span>🍽️</span><div><small>AI TABAK ANALİZİ</small><h2>{meal_name}</h2><p>Hedef: <b>{escape(goal)}</b> · Tüm değerler fotoğrafa dayalı yaklaşık tahminlerdir.</p></div></div>', unsafe_allow_html=True)

    totals = calculate_meal_totals(items)
    total_columns = st.columns(5, gap="small")
    total_cards = [
        ("Toplam kalori", totals.get("calories_kcal"), "kcal"),
        ("Protein", totals.get("protein_g"), "g"),
        ("Karbonhidrat", totals.get("carbohydrates_g"), "g"),
        ("Yağ", totals.get("fat_g"), "g"),
        ("Lif", totals.get("fiber_g"), "g"),
    ]
    for column, (label, value, unit) in zip(total_columns, total_cards):
        column.metric(label, _meal_value(value, unit))

    st.markdown("### Algılanan ürünler")
    for start in range(0, len(items), 3):
        columns = st.columns(3, gap="medium")
        for column, item in zip(columns, items[start:start + 3]):
            with column:
                st.markdown(
                    f'<div class="meal-item-card"><div class="meal-item-title">{escape(str(item.get("name") or "Adı okunamadı"))}</div><div class="meal-item-grams">Yaklaşık {_meal_value(item.get("estimated_grams"), "g")}</div><div class="meal-item-grid"><span>Kalori<b>{_meal_value(item.get("calories_kcal"), "kcal")}</b></span><span>Protein<b>{_meal_value(item.get("protein_g"), "g")}</b></span><span>Karbonhidrat<b>{_meal_value(item.get("carbohydrates_g"), "g")}</b></span><span>Yağ<b>{_meal_value(item.get("fat_g"), "g")}</b></span><span>Lif<b>{_meal_value(item.get("fiber_g"), "g")}</b></span><span>Güven<b>{escape(str(item.get("confidence") or "düşük").title())}</b></span></div></div>',
                    unsafe_allow_html=True,
                )

    with st.expander("Ürün adlarını veya gram miktarlarını düzenle", expanded=False):
        editor_rows = [{"Ürün adı": item.get("name", ""), "Tahmini gram": item.get("estimated_grams")} for item in items]
        editor_key = f"meal_editor_{photo_hash[:10]}_{goal}"
        edited_rows = st.data_editor(
            editor_rows,
            hide_index=True,
            num_rows="fixed",
            use_container_width=True,
            key=editor_key,
            column_config={
                "Ürün adı": st.column_config.TextColumn("Ürün adı", required=True),
                "Tahmini gram": st.column_config.NumberColumn("Tahmini gram", min_value=0.01, step=1.0, format="%.1f"),
            },
        )
        if st.button("Değerleri Güncelle", type="primary", key=f"update_meal_{photo_hash}_{goal}"):
            try:
                updated_items, warnings = scale_meal_items(items, edited_rows)
                active = st.session_state.meal_analysis_active_result
                active["data"]["items"] = updated_items
                st.session_state.meal_analysis_active_result = active
                st.session_state.meal_edit_warnings = warnings
                st.rerun()
            except MealAnalysisError as exc:
                st.error(str(exc))

    for warning in st.session_state.get("meal_edit_warnings", []):
        st.warning(warning)

    confidence = str(result.get("overall_confidence") or "düşük").title()
    st.markdown(f'<div class="meal-confidence">Genel analiz güveni: <strong>{escape(confidence)}</strong></div>', unsafe_allow_html=True)
    uncertainties = result.get("uncertainties") or []
    if uncertainties:
        with st.container(border=True):
            st.markdown("### 🔎 Belirsizlikler")
            for uncertainty in uncertainties:
                st.markdown(f"- {uncertainty}")
    if str(result.get("overall_confidence") or "").casefold() == "düşük":
        st.warning("Fotoğraf yeterince net olmayabilir. Daha aydınlık, yakın ve tabağın tamamını gösteren bir fotoğrafla tekrar deneyebilirsin.")

    with st.container(border=True):
        st.markdown("### 🎯 Hedefine Göre Değerlendirme")
        st.write(result.get("goal_comment") or "Bu görsel için hedefe göre güvenilir bir değerlendirme üretilemedi.")

    st.markdown('<div class="meal-disclaimer">Bu değerler yalnızca fotoğrafa dayalı yaklaşık tahminlerdir. Porsiyon miktarı, kullanılan yağ, soslar ve pişirme yöntemi sonucu değiştirebilir. Tıbbi veya diyetisyen tavsiyesi değildir.</div>', unsafe_allow_html=True)

    if "daily_meals" not in st.session_state:
        st.session_state.daily_meals = []
    analysis_id = build_analysis_id(photo_hash, goal)
    if st.button("Öğünüme Ekle", type="primary", key=f"add_daily_meal_{analysis_id}", use_container_width=True):
        record = {
            "analysis_id": analysis_id,
            "meal_name": result.get("meal_name") or "Analiz edilen öğün",
            "datetime": datetime.now().astimezone().isoformat(timespec="seconds"),
            "goal": goal,
            "items": items,
            "total_calories_kcal": totals.get("calories_kcal"),
            "total_protein_g": totals.get("protein_g"),
            "total_carbohydrates_g": totals.get("carbohydrates_g"),
            "total_fat_g": totals.get("fat_g"),
            "total_fiber_g": totals.get("fiber_g"),
            "image_hash": photo_hash,
        }
        try:
            if save_daily_meal(record):
                add_daily_meal(st.session_state.daily_meals, record)
                st.session_state.meal_added_message = "Öğün gününe eklendi."
            else:
                st.session_state.meal_added_message = "Bu öğün daha önce gününe eklendi."
        except DailyMealStoreError as exc:
            logger.warning("Öğün MongoDB'ye eklenemedi: %s", exc)
            st.error(str(exc))
    if st.session_state.get("meal_added_message"):
        st.success(st.session_state.meal_added_message)


def _render_meal_analysis() -> None:
    st.markdown('<div class="feature-section-intro"><span class="feature-section-icon">🍽️</span><div><h1>Tabağımı Analiz Et</h1><p>Tabağının fotoğrafını yükle, yaklaşık besin değerini hesaplayalım.</p></div></div>', unsafe_allow_html=True)
    valid_goals = ["Kilo Verme", "Dengeli Beslenme", "Kas Yapma"]
    current_goal = st.session_state.get("goal")
    if current_goal not in valid_goals:
        current_goal = "Dengeli Beslenme"
        st.session_state.goal = current_goal
    if "meal_analysis_goal_selector" not in st.session_state:
        st.session_state.meal_analysis_goal_selector = current_goal

    def sync_meal_goal() -> None:
        selected = st.session_state.meal_analysis_goal_selector
        st.session_state.goal = selected
        st.session_state.goal_selector = selected
        st.session_state.nutrition_label_goal_selector = selected
        st.session_state.video_goal_selector = selected

    st.markdown("#### Analiz hedefini seç")
    current_goal = st.radio(
        "Tabak analizi hedefi",
        valid_goals,
        horizontal=True,
        format_func=lambda option: f"{GOAL_ICONS[option]} {option}",
        key="meal_analysis_goal_selector",
        on_change=sync_meal_goal,
        label_visibility="collapsed",
    )
    st.session_state.goal = current_goal

    if "meal_analysis_cache" not in st.session_state:
        st.session_state.meal_analysis_cache = {}
    with st.container(key="meal_upload_card"):
        upload_copy, upload_action = st.columns([2.2, 1], gap="large", vertical_alignment="center")
        with upload_copy:
            st.markdown(
                '<div class="upload-card-copy"><div class="upload-card-icon">📷</div><div>'
                '<h3>Tabağının fotoğrafını yükle</h3>'
                '<p>Tabağın tamamını mümkün olduğunca net gösteren bir fotoğraf seç.</p>'
                '<small>🛡 Fotoğrafın yalnızca analiz için kullanılır.</small></div></div>',
                unsafe_allow_html=True,
            )
        with upload_action:
            uploaded_meal = st.file_uploader(
                "Fotoğraf Seç",
                type=["jpg", "jpeg", "png"],
                key="meal_photo_uploader",
                help="En fazla 10 MB boyutunda, tabağın tamamını net gösteren bir fotoğraf seç.",
                label_visibility="collapsed",
            )
    image_bytes = uploaded_meal.getvalue() if uploaded_meal is not None else b""
    photo_hash = hashlib.sha256(image_bytes).hexdigest() if image_bytes else ""
    if photo_hash:
        reset_meal_state_for_new_image(st.session_state, photo_hash)

    if uploaded_meal is not None:
        if len(image_bytes) > 10 * 1024 * 1024:
            st.warning("Fotoğraf en fazla 10 MB olabilir.")
            return
        preview_column, action_column = st.columns([1, 1.25], gap="large")
        with preview_column:
            preview_mime = uploaded_meal.type if uploaded_meal.type in {"image/jpeg", "image/png"} else "image/jpeg"
            preview_data = b64encode(image_bytes).decode("ascii")
            st.markdown(
                f'<div class="meal-preview-frame"><img src="data:{preview_mime};base64,{preview_data}" alt="Yüklenen tabak fotoğrafı"></div><div class="meal-preview-caption">Yüklenen tabak fotoğrafı</div>',
                unsafe_allow_html=True,
            )
        with action_column:
            st.markdown("#### Fotoğraf analize hazır")
            st.caption("Analiz yalnızca düğmeye bastığında başlar.")
            analyze_clicked = st.button("Tabağımı Analiz Et", type="primary", key="analyze_meal_photo", use_container_width=True)
    else:
        analyze_clicked = False

    cache_key = f"{photo_hash}:{current_goal}" if photo_hash else ""
    if analyze_clicked:
        st.session_state.pop("meal_analysis_error", None)
        st.session_state.pop("meal_added_message", None)
        if not image_bytes:
            st.session_state.meal_analysis_error = "Analiz etmek için önce bir tabak fotoğrafı yükle."
        else:
            try:
                with st.spinner("Tabağın analiz ediliyor..."):
                    result = get_or_analyze_meal(
                        st.session_state.meal_analysis_cache,
                        photo_hash,
                        current_goal,
                        lambda: analyze_meal_image(image_bytes, uploaded_meal.type, current_goal),
                    )
                st.session_state.meal_analysis_active_result = {"key": cache_key, "data": result}
            except MealAnalysisError as exc:
                st.session_state.meal_analysis_error = str(exc)

    if st.session_state.get("meal_analysis_error"):
        st.error(st.session_state.meal_analysis_error)
    active = st.session_state.get("meal_analysis_active_result", {})
    if active.get("key") == cache_key and isinstance(active.get("data"), dict):
        _render_meal_result(active["data"], current_goal, photo_hash)


def _daily_meal_totals(meals: list[dict]) -> dict[str, float]:
    fields = {
        "calories_kcal": "total_calories_kcal",
        "protein_g": "total_protein_g",
        "carbohydrates_g": "total_carbohydrates_g",
        "fat_g": "total_fat_g",
        "fiber_g": "total_fiber_g",
    }
    totals: dict[str, float] = {}
    for output_key, record_key in fields.items():
        total = 0.0
        for meal in meals:
            value = meal.get(record_key)
            try:
                total += float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                continue
        totals[output_key] = round(total, 1)
    return totals


def _meal_time_label(meal: dict) -> str:
    """Öğün kaydının saatini yerel saatle kullanıcıya uygun biçimde döndürür.

    Args:
        meal: ``datetime`` alanını içeren günlük öğün kaydı.

    Returns:
        str: ``14.30`` biçiminde saat veya saat okunamadığında açıklayıcı metin.
    """
    raw_datetime = str(meal.get("datetime") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw_datetime.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H.%M")
    except ValueError:
        return "Saat bilgisi yok"


def _daily_heading(selected_date: date, today: date) -> str:
    """Seçilen gün için Türkçe ve teknik olmayan bölüm başlığı üretir.

    Args:
        selected_date: Günüm ekranında açık olan yerel tarih.
        today: Kullanıcının bilgisayarındaki bugünün tarihi.

    Returns:
        str: Bugün için ``Bugün — 23 Ağustos``, geçmiş gün için tam tarih metni.
    """
    month_names = (
        "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
        "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
    )
    date_label = f"{selected_date.day} {month_names[selected_date.month - 1]}"
    if selected_date == today:
        return f"Bugün — {date_label}"
    return f"{date_label} {selected_date.year}"


def _render_daily_meals() -> None:
    st.markdown(
        '<div class="feature-section-intro"><span class="feature-section-icon">📅</span><div><h1>Beslenme Günlüğüm</h1>'
        '<p>Eklediğin öğünleri ve gün içindeki yaklaşık besin toplamlarını burada takip et.</p></div></div>',
        unsafe_allow_html=True,
    )
    if "daily_meals" not in st.session_state:
        st.session_state.daily_meals = []
    if "daily_completion_cache" not in st.session_state:
        st.session_state.daily_completion_cache = {}

    meals = st.session_state.daily_meals
    goal = st.session_state.get("goal", "Dengeli Beslenme")

    # Eski sahipsiz kayıtlar yalnızca giriş yapan kullanıcı açıkça onay verirse bağlanır.
    try:
        current_user_id = str(st.user.get("sub") or "").strip() if st.user.is_logged_in else ""
    except (AttributeError, KeyError, TypeError):
        current_user_id = ""
    if current_user_id:
        try:
            legacy_meal_count = count_claimable_legacy_daily_meals(current_user_id)
        except DailyMealStoreError as exc:
            logger.warning("Eski öğün kayıtları kontrol edilemedi: %s", exc)
            legacy_meal_count = 0
        if legacy_meal_count > 0:
            with st.container(border=True):
                st.info(f"Eski öğünler bulundu: {legacy_meal_count} kayıt")
                st.caption(
                    "Bunlar kullanıcı hesabı eklenmeden önce kaydedilmiş öğünlerdir. "
                    "Yalnızca onay verirsen bu Google hesabına bağlanır."
                )
                if st.button(
                    "Hesabıma Aktar",
                    type="primary",
                    key="claim_legacy_daily_meals",
                    use_container_width=True,
                ):
                    try:
                        claimed_count = claim_legacy_daily_meals_for_user(current_user_id)
                        st.session_state.daily_meals = load_daily_meals()
                        st.session_state.legacy_meal_claim_message = (
                            f"{claimed_count} eski öğün hesabına aktarıldı."
                            if claimed_count > 0
                            else "Aktarılacak yeni bir eski öğün bulunamadı."
                        )
                        st.rerun()
                    except DailyMealStoreError as exc:
                        logger.warning("Eski öğünler hesaba aktarılamadı: %s", exc)
                        st.error(str(exc))
    legacy_claim_message = st.session_state.pop("legacy_meal_claim_message", None)
    if legacy_claim_message:
        st.success(legacy_claim_message)
    st.markdown(f'<div class="label-current-goal">🎯 Seçili hedefin: <strong>{escape(goal)}</strong></div>', unsafe_allow_html=True)

    # Seçili tarih oturumda saklanır; sayfa ilk kez açıldığında yerel bugüne ayarlanır.
    today = datetime.now().astimezone().date()
    stored_date = st.session_state.get("daily_selected_date", today)
    if isinstance(stored_date, str):
        try:
            stored_date = date.fromisoformat(stored_date)
        except ValueError:
            stored_date = today
    selected_date = stored_date if isinstance(stored_date, date) else today
    st.session_state.daily_selected_date = selected_date

    # Gün değiştirme kontrolleri yalnızca ekranda açık olan günü değiştirir; kayıtlar korunur.
    previous, heading, following, return_today = st.columns([1.2, 2.2, 1.2, 1.2], gap="small")
    with previous:
        if st.button("← Önceki gün", key="daily_previous_day", use_container_width=True):
            st.session_state.daily_selected_date = selected_date - timedelta(days=1)
            st.rerun()
    with heading:
        st.markdown(f"### {_daily_heading(selected_date, today)}")
    with following:
        if st.button("Sonraki gün →", key="daily_next_day", use_container_width=True):
            st.session_state.daily_selected_date = selected_date + timedelta(days=1)
            st.rerun()
    with return_today:
        if st.button("Bugüne dön", key="daily_return_today", use_container_width=True):
            st.session_state.daily_selected_date = today
            st.rerun()

    # Seçili gün MongoDB'de tarih aralığıyla sorgulanır. Bağlantı geçici olarak
    # kullanılamazsa ekrandaki oturum verisiyle mevcut davranış korunur.
    try:
        selected_meals = load_daily_meals_for_date(selected_date)
    except DailyMealStoreError as exc:
        logger.warning("Seçili gün MongoDB'den okunamadı; oturum verisi kullanılıyor: %s", exc)
        selected_meals = meals_for_date(meals, selected_date)
    totals = _daily_meal_totals(selected_meals)
    total_columns = st.columns(5, gap="medium")
    for column, (label, key, unit) in zip(
        total_columns,
        [
            ("Günlük kalori", "calories_kcal", "kcal"),
            ("Protein", "protein_g", "g"),
            ("Karbonhidrat", "carbohydrates_g", "g"),
            ("Yağ", "fat_g", "g"),
            ("Lif", "fiber_g", "g"),
        ],
    ):
        column.metric(label, _meal_value(totals[key], unit))

    if not selected_meals:
        st.info("Bu tarihe ait eklenmiş bir öğün yok.")

    weekly = weekly_summary(meals)
    st.markdown("### Haftalık Özet")
    st.caption("Bugün dahil son 7 takvim günündeki kayıtlarına göre günlük ortalamalar.")
    average_columns = st.columns(4, gap="medium")
    for column, (label, key, unit) in zip(
        average_columns,
        [
            ("Ort. kalori", "calories_kcal", "kcal"),
            ("Ort. protein", "protein_g", "g"),
            ("Ort. karbonhidrat", "carbohydrates_g", "g"),
            ("Ort. yağ", "fat_g", "g"),
        ],
    ):
        column.metric(label, _meal_value(weekly["averages"][key], unit))

    st.markdown(
        f'<div class="weekly-summary-note">Son 7 günde toplam <strong>{weekly["meal_count"]}</strong> öğün kaydettin.</div>',
        unsafe_allow_html=True,
    )
    max_calories = max((float(day["calories_kcal"]) for day in weekly["days"]), default=0.0) or 1.0
    bar_markup = "".join(
        (
            '<div class="weekly-chart-bar">'
            f'<span>{float(day["calories_kcal"]):.0f} kcal</span>'
            f'<i style="height:{max(5, round((float(day["calories_kcal"]) / max_calories) * 100))}%"></i>'
            f'<small>{escape(str(day["label"]))}</small>'
            '</div>'
        )
        for day in weekly["days"]
    )
    st.markdown(
        '<div class="weekly-chart"><h4>Günlere göre kalori</h4>'
        f'<div class="weekly-chart-bars">{bar_markup}</div></div>',
        unsafe_allow_html=True,
    )

    weekly_signature = json.dumps(weekly["days"], ensure_ascii=False, sort_keys=True)
    weekly_comment_key = hashlib.sha256(f"{goal}:{weekly_signature}".encode("utf-8")).hexdigest()
    if "weekly_comment_cache" not in st.session_state:
        st.session_state.weekly_comment_cache = {}
    st.markdown("### AI ile Haftamı Yorumla")
    st.caption("Yapay zekâ hedefin ve haftalık yaklaşık toplamların için kısa, genel bir değerlendirme yapar.")
    if weekly["meal_count"] == 0:
        st.info("Son 7 günde analiz edilip eklenmiş bir öğün bulunmuyor.")
    elif st.button("AI ile Haftamı Yorumla", type="primary", key="comment_my_week", use_container_width=True):
        if weekly_comment_key not in st.session_state.weekly_comment_cache:
            with st.spinner("Haftan değerlendiriliyor..."):
                comment = comment_on_weekly_summary(goal, weekly)
            if comment:
                st.session_state.weekly_comment_cache[weekly_comment_key] = comment
            else:
                st.warning("Şu anda haftalık AI yorumu alınamadı. API anahtarını, kotanı ve bağlantını kontrol edip tekrar deneyebilirsin.")
    weekly_comment = st.session_state.weekly_comment_cache.get(weekly_comment_key)
    if weekly_comment:
        with st.container(border=True):
            st.markdown("#### Haftalık AI Yorumu")
            st.write(weekly_comment)
            st.caption("Bu yorum yaklaşık kayıtlara dayanır; tıbbi veya diyetetik tavsiye değildir.")

    st.markdown("### Eklenen öğünler")
    # Liste yalnızca yukarıda seçilen güne ait kayıtları gösterir.
    for index, meal in enumerate(selected_meals):
        meal_name = escape(str(meal.get("meal_name") or "Analiz edilen öğün"))
        timestamp = _meal_time_label(meal)
        item_names = ", ".join(
            str(item.get("name") or "") for item in meal.get("items", []) if isinstance(item, dict)
        ) or "Ürün bilgisi yok"
        details, remove = st.columns([6, 1], vertical_alignment="center")
        with details:
            st.markdown(
                f'<div class="meal-item-card"><div class="meal-item-title">{meal_name}</div>'
                f'<div class="meal-item-grams">{escape(timestamp)}</div>'
                f'<div class="meal-item-grams">{escape(item_names)}</div>'
                f'<div class="meal-item-grid"><span>Kalori<b>{_meal_value(meal.get("total_calories_kcal"), "kcal")}</b></span>'
                f'<span>Protein<b>{_meal_value(meal.get("total_protein_g"), "g")}</b></span>'
                f'<span>Karbonhidrat<b>{_meal_value(meal.get("total_carbohydrates_g"), "g")}</b></span>'
                f'<span>Yağ<b>{_meal_value(meal.get("total_fat_g"), "g")}</b></span></div></div>',
                unsafe_allow_html=True,
            )
        with remove:
            if st.button("Sil", key=f"remove_daily_meal_{meal.get('analysis_id', index)}", use_container_width=True):
                analysis_id = str(meal.get("analysis_id") or "")
                if not analysis_id:
                    st.warning("Bu öğün kaydı silinemedi.")
                else:
                    try:
                        if delete_daily_meal(analysis_id):
                            # Seçili günün geçici sıra numarası yerine benzersiz kimlikle doğru kayıt silinir.
                            st.session_state.daily_meals = [
                                record
                                for record in st.session_state.daily_meals
                                if str(record.get("analysis_id") or "") != analysis_id
                            ]
                            st.session_state.daily_completion_message = "Öğün gününden kaldırıldı."
                            st.rerun()
                        else:
                            st.warning("Bu öğün daha önce kaldırılmış olabilir.")
                    except DailyMealStoreError as exc:
                        logger.warning("Öğün MongoDB'den silinemedi: %s", exc)
                        st.error(str(exc))

    if st.session_state.get("daily_completion_message"):
        st.success(st.session_state.daily_completion_message)

    # AI önerisi yalnızca seçili günün toplamlarına ait olacak biçimde önbellek anahtarı oluşturulur.
    meal_signature = "|".join(str(meal.get("analysis_id") or "") for meal in selected_meals)
    completion_key = hashlib.sha256(
        f"{goal}:{selected_date.isoformat()}:{meal_signature}".encode("utf-8")
    ).hexdigest()
    st.markdown("### AI ile Günümü Tamamla")
    st.caption("Yapay zekâ, hedefin ve seçili günün yaklaşık toplamlarına göre yalnızca bir sonraki öğün için kısa bir fikir verir.")
    if st.button(
        "AI ile Günümü Tamamla",
        type="primary",
        key="complete_my_day",
        use_container_width=True,
        disabled=not selected_meals,
    ):
        if completion_key not in st.session_state.daily_completion_cache:
            with st.spinner("Günün değerlendiriliyor..."):
                suggestion = suggest_next_meal(
                    goal,
                    totals,
                    [str(meal.get("meal_name") or "Öğün") for meal in selected_meals],
                )
            if suggestion:
                st.session_state.daily_completion_cache[completion_key] = suggestion
            else:
                st.warning("Şu anda yapay zekâ önerisi alınamadı. API anahtarını, kotanı ve bağlantını kontrol edip tekrar deneyebilirsin.")

    suggestion = st.session_state.daily_completion_cache.get(completion_key)
    if suggestion:
        with st.container(border=True):
            st.markdown("#### Sonraki öğün için öneri")
            st.write(suggestion)
            st.caption("Bu öneri yaklaşık gün toplamlarına dayanır; tıbbi veya diyetetik tavsiye değildir.")


def render_discover(search: Callable[[list[str], str], None], navigate: Callable[[str], None]) -> None:
    if st.button("← Geri Dön", key="discover_back_home"):
        navigate("Ana Sayfa")
        st.rerun()
    section_options = ["🍲 Malzemelerimden Tarif Bul", "🏷️ Besin Etiketi Analizi", "▶️ Hedefime Uygun Videolar", "🍽️ Tabağımı Analiz Et", "📅 Beslenme Günlüğüm"]
    if st.session_state.get("discover_section") not in section_options:
        st.session_state.discover_section = section_options[0]
    if st.session_state.get("discover_section_switch") not in section_options:
        st.session_state.discover_section_switch = st.session_state.discover_section
    st.markdown('<div class="discover-section-title">Beslenme Asistanım</div>', unsafe_allow_html=True)
    with st.container(key="discover_section_nav"):
        selected_section = st.radio(
            "",
            section_options,
            key="discover_section_switch",
            horizontal=True,
            label_visibility="collapsed",
        )
    if selected_section != st.session_state.discover_section:
        st.session_state.discover_section = selected_section
        st.rerun()
    active_section = st.session_state.discover_section
    if active_section == section_options[0]:
        _render_ingredient_discover(search, navigate)
    elif active_section == section_options[1]:
        _render_nutrition_label_analysis()
    elif active_section == section_options[2]:
        _render_workout_videos(st.session_state.get("goal", "Dengeli Beslenme"))
    elif active_section == section_options[3]:
        _render_meal_analysis()
    else:
        _render_daily_meals()


def _render_ingredient_discover(search: Callable[[list[str], str], None], navigate: Callable[[str], None]) -> None:

    st.markdown(
        '<div class="feature-section-intro"><span class="feature-section-icon">🥗</span><div><h1>Malzemelerini Gir</h1>'
        '<p>Elindeki malzemeleri fotoğraftan veya elle ekle; hedefinle uyumlu tarifleri birlikte bulalım.</p></div></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<p class="muted">Elindeki malzemeleri ekleyerek sana uygun tarifleri bulmamıza yardımcı ol.</p>', unsafe_allow_html=True)

    if "image_analysis_cache" not in st.session_state:
        st.session_state.image_analysis_cache = {}

    with st.container(key="ingredient_upload_card"):
        upload_copy, upload_action = st.columns([2.2, 1], gap="large", vertical_alignment="center")
        with upload_copy:
            st.markdown(
                '<div class="upload-card-copy"><div class="upload-card-icon">📷</div><div>'
                '<h3>Yiyecek fotoğrafını yükle</h3>'
                '<p>Buzdolabının veya yiyeceklerinin net bir fotoğrafını seç.</p>'
                '<small>🛡 Fotoğrafın yalnızca analiz için kullanılır.</small></div></div>',
                unsafe_allow_html=True,
            )
        with upload_action:
            uploaded_photo = st.file_uploader(
                "Fotoğraf Seç",
                type=["jpg", "jpeg", "png"],
                key="ingredient_photo_uploader",
                help="JPG, JPEG veya PNG biçiminde bir fotoğraf yükleyebilirsin.",
                label_visibility="collapsed",
            )
        if uploaded_photo is not None:
            photo_bytes = uploaded_photo.getvalue()
            if len(photo_bytes) > 10 * 1024 * 1024:
                st.warning("Fotoğraf en fazla 10 MB olabilir.")
            else:
                preview, action = st.columns([1.5, 1], gap="large")
                with preview:
                    st.image(photo_bytes, caption="Yüklenen fotoğraf", use_container_width=True)
                with action:
                    st.markdown("**Fotoğraf hazır**")
                    st.caption("Analiz yalnızca aşağıdaki butona bastığında başlar.")
                    analyze_clicked = st.button(
                        "AI ile Malzemeleri Tanı",
                        key="analyze_ingredient_photo",
                        type="primary",
                        use_container_width=True,
                    )
                if analyze_clicked:
                    photo_hash = hashlib.sha256(photo_bytes).hexdigest()
                    cached_ingredients = st.session_state.image_analysis_cache.get(photo_hash)
                    analysis_error = None
                    if cached_ingredients is None:
                        try:
                            with st.spinner("Fotoğraftaki malzemeler tanınıyor..."):
                                cached_ingredients = analyze_food_image(photo_bytes, uploaded_photo.type)
                        except GeminiServiceError as exc:
                            analysis_error = str(exc)
                            cached_ingredients = None
                        if cached_ingredients is not None:
                            st.session_state.image_analysis_cache[photo_hash] = cached_ingredients
                    if analysis_error:
                        st.error(analysis_error)
                    elif cached_ingredients is None:
                        st.error("Fotoğraf şu anda analiz edilemedi. Lütfen tekrar dene.")
                    elif not cached_ingredients:
                        st.info("Fotoğrafta açıkça tanınabilen bir yiyecek bulunamadı.")
                    else:
                        existing = {str(value).strip().casefold() for value in st.session_state.ingredients}
                        for detected in cached_ingredients:
                            if detected.casefold() not in existing:
                                st.session_state.ingredients.append(detected.title())
                                existing.add(detected.casefold())
                        st.session_state.last_image_ingredients = cached_ingredients
                        st.rerun()

        last_detected = st.session_state.get("last_image_ingredients", [])
        if last_detected:
            st.success("Fotoğraftan bulunanlar: " + ", ".join(str(item).title() for item in last_detected))

    st.markdown("#### Ya da malzemelerini elle ekle")

    # Formun clear_on_submit özelliği, Enter'dan sonra metin alanını tarayıcı
    # tarafında da sıfırlar. Böylece sonraki malzeme önceki metne eklenmez.
    with st.form("ingredient_form", clear_on_submit=True):
        ingredient_columns = st.columns([8, 1], gap="small")
        ingredient_value = ingredient_columns[0].text_input(
            "Malzeme",
            placeholder="Malzemeleri virgülle ayırıp Enter'a bas...",
            label_visibility="collapsed",
            key="ingredient_form_input",
        )
        ingredient_submitted = ingredient_columns[1].form_submit_button(
            "Listeye Ekle",
            type="primary",
            use_container_width=True,
        )

    if ingredient_submitted:
        existing = {value.strip().casefold() for value in st.session_state.ingredients}
        entered_ingredients = [
            re.sub(r"[\u200b-\u200d\ufeff]", "", item).strip(" ,;\t\r\n")
            for item in re.split(r"[,;\n]+", ingredient_value)
            if re.sub(r"[\u200b-\u200d\ufeff]", "", item).strip(" ,;\t\r\n")
        ]
        for new_ingredient in entered_ingredients:
            normalized = new_ingredient.casefold()
            if normalized not in existing:
                st.session_state.ingredients.append(new_ingredient)
                existing.add(normalized)
        st.rerun()

    quick = ["Yumurta", "Domates", "Peynir", "Tavuk", "Salatalık", "Zeytinyağı"]
    quick_columns = st.columns(3)
    for index, item in enumerate(quick):
        if quick_columns[index % 3].button(f"+ {item}", key=f"quick_{item}", use_container_width=True):
            if item.lower() not in [value.lower() for value in st.session_state.ingredients]:
                st.session_state.ingredients.append(item)

    combined: list[str] = []
    seen_ingredients: set[str] = set()
    for item in st.session_state.ingredients:
        cleaned_item = re.sub(r"[\u200b-\u200d\ufeff]", "", str(item)).strip(" ,;\t\r\n")
        normalized = cleaned_item.casefold()
        if normalized and any(character.isalnum() for character in cleaned_item) and normalized not in seen_ingredients:
            seen_ingredients.add(normalized)
            combined.append(cleaned_item)
    if combined:
        with st.container(border=True):
            st.markdown('<span class="ingredient-summary-title">Seçtiğin malzemeler</span>', unsafe_allow_html=True)
            chip_columns = st.columns(min(4, len(combined)), gap="small")
            ingredient_to_remove = ""
            for index, item in enumerate(combined):
                if chip_columns[index % len(chip_columns)].button(
                    f"✓ {item}  ×",
                    key=f"remove_ingredient_{index}_{hashlib.sha1(item.casefold().encode('utf-8')).hexdigest()[:8]}",
                    help=f"{item} malzemesini kaldır",
                    use_container_width=True,
                ):
                    ingredient_to_remove = item
            if ingredient_to_remove:
                remove_key = ingredient_to_remove.casefold()
                st.session_state.ingredients = [
                    item for item in st.session_state.ingredients
                    if str(item).strip().casefold() != remove_key
                ]
                st.rerun()
    st.markdown("### Beslenme Hedefini Seç")
    descriptions = {
        "Kilo Verme": "Düşük kalorili, dengeli ve doyurucu tarifler.",
        "Dengeli Beslenme": "Dengeli besin değerlerine sahip sağlıklı tarifler.",
        "Kas Yapma": "Yüksek proteinli, kas gelişimini destekleyen tarifler.",
    }
    icons = {"Kilo Verme": "🌿", "Dengeli Beslenme": "⚖️", "Kas Yapma": "💪"}
    options = list(descriptions)

    def format_goal(option: str) -> str:
        return f"{icons[option]}  {option}\n{descriptions[option]}"

    # Radio kartları, hedef seçimi sırasında buton DOM'unun yeniden
    # oluşturulmasından kaynaklanan Streamlit removeChild hatasını engeller.
    def sync_recipe_goal() -> None:
        selected = st.session_state.goal_selector
        st.session_state.goal = selected
        st.session_state.nutrition_label_goal_selector = selected
        st.session_state.meal_analysis_goal_selector = selected
        st.session_state.video_goal_selector = selected

    selected_goal = st.radio(
        "Beslenme hedefi",
        options,
        index=options.index(st.session_state.goal),
        format_func=format_goal,
        label_visibility="collapsed",
        key="goal_selector",
        on_change=sync_recipe_goal,
    )
    st.session_state.goal = selected_goal
    goal = selected_goal

    if not combined:
        st.info("Malzemelerini yazdıktan sonra Enter'a veya “Listeye Ekle” butonuna bas.")
    if st.button(
        "Tarifleri Keşfet →",
        type="primary",
        use_container_width=True,
        disabled=not combined,
        key="discover_recipes_submit",
    ):
        search(combined, goal)
        if st.session_state.page == "Sonuçlar":
            st.rerun()


def _clean_missing_ingredient(item: object) -> str:
    """API/çeviri servisinden gelen hatalı malzeme adlarını ekranda düzelt."""
    value = str(item).strip()
    corrections = {
        "kişisel tava": "pişirme spreyi",
        "kişisel pan": "pişirme spreyi",
        "personal pan": "pişirme spreyi",
        "cooking spray": "pişirme spreyi",
    }
    return corrections.get(value.casefold(), value)


def _recipe_card(recipe: dict, goal: str, show_score: bool = True, card_variant: str = "") -> str:
    name = escape(str(recipe.get("name", "İsimsiz tarif")))
    primary_image, fallback_image = _recipe_image_sources(recipe)
    image = escape(primary_image, quote=True)
    fallback = escape(fallback_image, quote=True)
    image_html = (
        f'<img src="{image}" alt="{name}" loading="lazy" '
        f'onerror="this.onerror=null;this.src=\'{fallback}\';">'
    )
    missing = recipe.get("missing", [])
    missing_text = ", ".join(escape(_clean_missing_ingredient(item)) for item in missing[:4]) if missing else "Eksik malzeme yok"
    label = escape(str(recipe.get("label", "Yerel tarif")))
    detail = escape(str(recipe.get("detail", f"{recipe.get('used', 0)}/{recipe.get('total', 0)} malzeme uyumu")))
    missing_html = f'<div class="missing" translate="no"><strong>Eksikler:</strong> {missing_text}</div>' if recipe.get("show_missing", True) and missing else ""
    # Uyum puanı sıralamada arka planda kullanılabilir; arayüzde gösterilmez.
    card_class = "recipe-card recipe-card--library"
    if card_variant == "home":
        card_class += " home-recipe-card"
    return f"""
    <div class="{card_class}">
      {image_html}
      <div class="recipe-body">
        <div class="recipe-type">{detail}</div>
        <div class="recipe-name">{name}</div>
        <div class="recipe-meta"><span class="nutrition"><span class="meta-icon">♨</span> {recipe.get('calories', 0):.0f} kcal</span><span class="nutrition"><span class="meta-icon">♧</span> {recipe.get('protein', 0):.0f} g protein</span></div>
        {missing_html}
      </div>
    </div>
    """


def _recipe_grid(
    recipes: list[dict],
    goal: str,
    navigate: Callable[[str], None] | None = None,
    show_score: bool = True,
    detail_context: str = "library",
    card_variant: str = "",
) -> None:
    for start in range(0, len(recipes), 3):
        columns = st.columns(3, gap="medium")
        for column, recipe in zip(columns, recipes[start:start + 3]):
            with column:
                st.markdown(
                    _recipe_card(recipe, goal, show_score=show_score, card_variant=card_variant),
                    unsafe_allow_html=True,
                )
                # Streamlit karttan sonra düğmeyi ayrı bir eleman olarak
                # çiziyor. Kısa başlıklı ana sayfa kartlarına küçük bir
                # dengeleme boşluğu ekleyerek düğmeleri aynı hizada tutarız.
                if card_variant == "home":
                    recipe_name = str(recipe.get("name", ""))
                    spacer_height = 28 if len(recipe_name) <= 42 else 0
                    st.markdown(
                        f'<div class="home-recipe-button-spacer" style="height:{spacer_height}px"></div>',
                        unsafe_allow_html=True,
                    )
                if navigate and st.button("Tarifi İncele", key=f"detail_{recipe.get('id', recipe.get('name', start))}", use_container_width=True):
                    st.session_state.selected_recipe = recipe
                    st.session_state.selected_recipe_context = detail_context
                    navigate("Tarif Detayı")
                    st.rerun()


def _render_workout_videos(goal: str) -> None:
    st.markdown('<div class="feature-section-intro"><span class="feature-section-icon">▶</span><div><h1>Hedefini Destekleyen Videolar</h1><p>Beslenme hedefini hareketle desteklemek için seviyeni seç.</p></div></div>', unsafe_allow_html=True)
    valid_goals = ["Kilo Verme", "Dengeli Beslenme", "Kas Yapma"]
    if goal not in valid_goals:
        goal = "Dengeli Beslenme"
    if "video_goal_selector" not in st.session_state:
        st.session_state.video_goal_selector = goal

    def sync_video_goal() -> None:
        selected = st.session_state.video_goal_selector
        st.session_state.goal = selected
        st.session_state.goal_selector = selected
        st.session_state.nutrition_label_goal_selector = selected
        st.session_state.meal_analysis_goal_selector = selected

    st.markdown("#### Video hedefini seç")
    goal = st.radio(
        "Video hedefi",
        valid_goals,
        horizontal=True,
        format_func=lambda option: f"{GOAL_ICONS[option]} {option}",
        key="video_goal_selector",
        on_change=sync_video_goal,
        label_visibility="collapsed",
    )
    st.session_state.goal = goal
    st.markdown(f'<div class="workout-current-goal">🎯 Mevcut hedefin: <strong>{escape(goal)}</strong></div>', unsafe_allow_html=True)

    controls = st.columns([2, 1, 4], gap="medium", vertical_alignment="bottom")
    level = controls[0].selectbox(
        "Seviyen",
        ["Başlangıç", "Orta", "İleri"],
        key="youtube_workout_level",
    )
    search_clicked = controls[1].button(
        "Bana Video Öner",
        type="primary",
        key="youtube_workout_search",
        use_container_width=True,
    )

    result_key = f"{goal}|{level}"
    if "youtube_video_results" not in st.session_state:
        st.session_state.youtube_video_results = {}
    if "youtube_video_errors" not in st.session_state:
        st.session_state.youtube_video_errors = {}

    if search_clicked:
        st.session_state.youtube_video_errors.pop(result_key, None)
        st.session_state.youtube_video_results.pop(result_key, None)
        try:
            with st.spinner("Hedefine uygun videolar aranıyor..."):
                videos = search_youtube_videos(goal, level)
            st.session_state.youtube_video_results[result_key] = videos
            st.session_state.youtube_video_errors.pop(result_key, None)
        except YouTubeServiceError as exc:
            st.session_state.youtube_video_errors[result_key] = str(exc)

    error = st.session_state.youtube_video_errors.get(result_key)
    videos = st.session_state.youtube_video_results.get(result_key, [])
    if error:
        st.warning(error)
        return
    if not videos:
        st.markdown('<div class="workout-empty">Seviyeni seçip <b>Bana Video Öner</b> düğmesine bastığında en fazla 3 öneri burada görünecek.</div>', unsafe_allow_html=True)
        return

    video_columns = st.columns(len(videos), gap="medium")
    for column, video in zip(video_columns, videos):
        with column:
            with st.container(border=True):
                st.markdown(f'<div class="workout-video-title">{escape(video.get("title", "Video"))}</div>', unsafe_allow_html=True)
                st.markdown(f'<div class="workout-channel">{escape(video.get("channel", "YouTube"))}</div>', unsafe_allow_html=True)
                st.video(video["url"])
                st.link_button("YouTube'da Aç ↗", video["url"], use_container_width=True)


def render_recipe_results(recipes: list[dict], goal: str, navigate: Callable[[str], None]) -> None:
    if st.button("← Seçimlere Geri Dön", key="back_to_discover_from_results"):
        navigate("Tarifimi Keşfet")
        st.rerun()
    st.title("Sana Özel Tarifler")
    st.markdown(
        f'<div class="result-goal-banner"><span>🎯</span><div><strong>{escape(goal)} hedefine uygun tarifler</strong><small>Seçtiğin malzemelere göre en uyumlu sonuçlar öne çıkarıldı.</small></div></div>',
        unsafe_allow_html=True,
    )
    selected_ingredients = []
    seen_ingredients: set[str] = set()
    for item in st.session_state.get("ingredients", []):
        cleaned = re.sub(r"[\u200b-\u200d\ufeff]", "", str(item)).strip(" ,;\t\r\n")
        normalized = cleaned.casefold()
        if cleaned and normalized not in seen_ingredients:
            seen_ingredients.add(normalized)
            selected_ingredients.append(cleaned)
    if selected_ingredients:
        ingredient_chips = "".join(
            f'<span class="result-ingredient-chip">✓ {escape(item)}</span>'
            for item in selected_ingredients
        )
        st.markdown(
            f'<div class="result-ingredients"><span class="result-ingredients-title">Seçtiğin malzemeler</span>{ingredient_chips}</div>',
            unsafe_allow_html=True,
        )
    if not recipes:
        st.info("Yeterince eşleşen tarif bulunamadı. Birkaç malzeme daha eklemeyi dene.")
        if st.button("Malzeme ve Hedefi Değiştir"):
            navigate("Tarifimi Keşfet")
            st.rerun()
        return

    actions = st.columns([1, 1, 3])
    if actions[0].button("Malzeme ve Hedefi Değiştir"):
        navigate("Tarifimi Keşfet")
        st.rerun()
    if actions[1].button("Asistana Sor", type="primary"):
        navigate("Asistan")
        st.rerun()
    st.markdown("<br>", unsafe_allow_html=True)
    _recipe_grid(recipes, goal, navigate, detail_context="personalized")


def _recipe_category(recipe: dict) -> str:
    text = f"{recipe.get('name', '')} {recipe.get('detail', '')}".casefold()
    if "çorba" in text:
        return "Çorbalar"
    if "salata" in text or "bowl" in text:
        return "Salatalar"
    if any(word in text for word in ("omlet", "menemen", "pancake", "frittata", "yumurta", "tost", "kahvaltı")):
        return "Kahvaltı"
    if any(word in text for word in ("smoothie", "puding", "enerji", "meyve kasesi", "humus", "atıştırmalık")):
        return "Atıştırmalık"
    return "Ana Yemekler"


def render_recipes(recipes: list[dict], navigate: Callable[[str], None]) -> None:
    st.title("Tarifler")
    st.markdown('<p class="muted">Öne çıkan tarifleri keşfet veya aradığın lezzeti kolayca bul.</p>', unsafe_allow_html=True)
    if not recipes:
        recipes = FEATURED_RECIPES
    if not recipes:
        st.info("Tarif listesi arama yaptıktan sonra oluşur.")
        if st.button("Tarifimi Keşfet", type="primary"):
            navigate("Tarifimi Keşfet")
            st.rerun()
        return
    search_column, category_column, sort_column = st.columns([2.4, 1, 1], gap="small")
    query = search_column.text_input("Tarif ara", placeholder="Tarif ara...", label_visibility="collapsed")
    category = category_column.selectbox(
        "Kategori",
        ["Tüm Kategoriler", "Kahvaltı", "Ana Yemekler", "Çorbalar", "Salatalar", "Atıştırmalık"],
        label_visibility="collapsed",
    )
    sorting = sort_column.selectbox(
        "Sıralama",
        ["Önerilen sıra", "Ada göre A–Z", "Kalorisi düşük", "Proteini yüksek"],
        label_visibility="collapsed",
    )

    filtered = [recipe for recipe in recipes if query.casefold() in recipe.get("name", "").casefold()]
    if category != "Tüm Kategoriler":
        filtered = [recipe for recipe in filtered if _recipe_category(recipe) == category]

    if sorting == "Ada göre A–Z":
        filtered.sort(key=lambda recipe: str(recipe.get("name", "")).casefold())
    elif sorting == "Kalorisi düşük":
        filtered.sort(key=lambda recipe: float(recipe.get("calories", 0) or 0))
    elif sorting == "Proteini yüksek":
        filtered.sort(key=lambda recipe: float(recipe.get("protein", 0) or 0), reverse=True)
    if not filtered:
        st.info("Aradığın ölçütlere uygun tarif bulunamadı.")
        return

    filter_state = (query, category, sorting)
    if st.session_state.get("recipes_filter_state") != filter_state:
        st.session_state.recipes_filter_state = filter_state
        st.session_state.library_visible_count = 9
        # Streamlit'in unsafe HTML kartlarındaki eski besin değerlerini
        # korumaması için filtre değişiminden sonra temiz bir çizim yap.
        st.rerun()

    visible_count = min(st.session_state.get("library_visible_count", 9), len(filtered))
    st.markdown("### Öne Çıkan Tarifler" if not query and category == "Tüm Kategoriler" else "### Tarifler")
    _recipe_grid(
        filtered[:visible_count],
        st.session_state.get("goal", "Dengeli Beslenme"),
        navigate,
        show_score=False,
        detail_context="library",
    )

    if visible_count < len(filtered):
        button_columns = st.columns([2, 1, 2])
        if button_columns[1].button("Daha Fazla Göster ↓", use_container_width=True):
            st.session_state.library_visible_count = visible_count + 9
            st.rerun()


def _local_instructions(recipe: dict) -> list[str]:
    name = str(recipe.get("name", "")).lower()
    if "salata" in name or "bowl" in name:
        return ["Malzemeleri yıka ve doğra.", "Tüm malzemeleri bir kasede birleştir.", "Zeytinyağı ve baharat ekleyip karıştır."]
    if "çorba" in name:
        return ["Sebzeleri doğrayıp tencereye al.", "Üzerine su ve baharat ekleyerek yumuşayana kadar pişir.", "Çorbayı blenderdan geçirip sıcak servis et."]
    if "smoothie" in name or "puding" in name:
        return ["Tüm malzemeleri blendera veya kaseye al.", "Pürüzsüz kıvam alana kadar karıştır.", "Soğuk servis et."]
    if "omlet" in name or "menemen" in name or "pancake" in name:
        if "omlet" in name:
            return ["Yumurtaları bir kasede çırpın ve bir tutam tuz ekleyin.", "Domates ve biberi küçük küçük doğrayın.", "Sebzeleri az zeytinyağıyla 3-4 dakika soteleyin.", "Çırpılmış yumurtayı tavaya ekleyin ve kısık ateşte pişirin.", "Altı pişince omleti ikiye katlayıp sıcak servis edin."]
        return ["Malzemeleri doğrayıp hazırlayın.", "Yumurta ve diğer malzemeleri karıştırın.", "Orta ateşte pişirip sıcak servis edin."]
    return ["Malzemeleri hazırlayıp doğrayın.", "Malzemeleri uygun sırayla tavada veya fırında pişirin.", "Baharatları ekleyip sıcak servis edin."]


def _suggested_ingredient_amount(ingredient: str) -> str:
    """Yerel tariflerde iki kişilik, kullanıcıya yol gösteren yaklaşık ölçü üret."""
    normalized = ingredient.strip().casefold()
    amounts = {
        "tavuk": "300 g tavuk göğsü", "hindi": "300 g hindi eti", "hindi kıyma": "300 g hindi kıyma",
        "kıyma": "250 g kıyma", "et": "300 g kuşbaşı et", "somon": "2 parça somon fileto",
        "ton balığı": "1 büyük kutu ton balığı", "karides": "300 g ayıklanmış karides", "tofu": "250 g tofu",
        "yumurta": "3 adet yumurta", "domates": "2 orta boy domates", "biber": "2 adet biber",
        "salatalık": "1 büyük salatalık", "soğan": "1 orta boy soğan", "sarımsak": "2 diş sarımsak",
        "patates": "2 orta boy patates", "kabak": "2 orta boy kabak", "havuç": "1 orta boy havuç",
        "avokado": "1 adet avokado", "limon": "1 adet limon", "marul": "4-5 yaprak marul",
        "sebze": "2 su bardağı doğranmış karışık sebze", "yeşillik": "1 avuç taze yeşillik",
        "maydanoz": "Yarım demet maydanoz", "semizotu": "1 bağ semizotu", "taze fasulye": "500 g taze fasulye",
        "peynir": "100 g peynir", "yoğurt": "1 su bardağı yoğurt", "süt": "1 su bardağı süt",
        "kefir": "1 su bardağı kefir", "tereyağı": "1 yemek kaşığı tereyağı", "zeytinyağı": "2 yemek kaşığı zeytinyağı",
        "pirinç": "1 su bardağı pirinç", "bulgur": "1 su bardağı ince bulgur", "kinoa": "1 su bardağı kinoa",
        "makarna": "200 g makarna", "yulaf": "1 su bardağı yulaf", "mercimek": "1 su bardağı kırmızı mercimek",
        "nohut": "1,5 su bardağı haşlanmış nohut", "ekmek": "4 dilim ekmek", "chia tohumu": "3 yemek kaşığı chia tohumu",
        "muz": "1 büyük muz", "çilek": "1 su bardağı çilek", "meyve": "1,5 su bardağı doğranmış meyve",
        "fıstık ezmesi": "2 yemek kaşığı fıstık ezmesi", "tahin": "2 yemek kaşığı tahin",
        "salça": "1 yemek kaşığı salça", "baharat": "Tuz, karabiber ve sevilen baharatlar",
    }
    return amounts.get(normalized, f"{ingredient.title()} — damak tadına göre")


def _detailed_local_instructions(recipe: dict) -> list[str]:
    """Kısa yerel adımları uygulanabilir bir pişirme akışına dönüştür."""
    name = str(recipe.get("name", "")).casefold()
    base = get_recipe_instructions(str(recipe.get("name", ""))) or _local_instructions(recipe)
    opening = "Başlamadan önce tüm malzemeleri tezgâha çıkarın; sebzeleri yıkayın ve ölçüleri hazırlayın."
    if "tavuk" in name or "hindi" in name:
        safety = "Etin en kalın kısmını kontrol edin; içi pembe kalmamalı ve tamamen pişmiş olmalıdır."
    elif "balık" in name or "somon" in name or "karides" in name:
        safety = "Deniz ürününü fazla kurutmadan, rengi tamamen değişip içi opaklaşana kadar pişirin."
    elif "çorba" in name:
        safety = "Kıvam koyuysa azar azar sıcak su ekleyin; tuzunu ocağı kapatmadan önce kontrol edin."
    elif "omlet" in name or "menemen" in name or "yumurta" in name:
        safety = "Yumurtayı kısık-orta ateşte, kurumadan fakat çiğ kısmı kalmayacak şekilde pişirin."
    else:
        safety = "Pişirme sonunda kıvamı ve baharatı kontrol edip gerekiyorsa küçük eklemeler yapın."
    serving = "Yemeği 2 tabağa paylaştırın ve sıcak servis edin. Artanı soğuduktan sonra kapalı kapta buzdolabına kaldırın."
    return [opening, *base, safety, serving]


def _recipe_times(recipe: dict) -> tuple[int, int]:
    api_preparation = int(recipe.get("preparation_minutes") or 0)
    api_cooking = int(recipe.get("cooking_minutes") or 0)
    if api_preparation or api_cooking:
        return api_preparation, api_cooking
    total = int(recipe.get("ready_in_minutes") or 0)
    if total:
        preparation = max(5, min(20, total // 3))
        return preparation, max(5, total - preparation)
    name = str(recipe.get("name", "")).casefold()
    if "salata" in name or "smoothie" in name or "sandviç" in name:
        return 10, 5
    if "çorba" in name or "fırında" in name or "güveç" in name:
        return 15, 35
    return 10, 20


def _translate_instruction_html(instructions: str) -> str:
    """API'den gelen İngilizce adımları çevirirken HTML etiketlerini koru."""
    parts = re.split(r"(<[^>]+>)", str(instructions))
    translated: list[str] = []
    for part in parts:
        if not part or re.fullmatch(r"<[^>]+>", part):
            translated.append(part)
        elif part.strip():
            translated.append(translate_to_turkish(part))
        else:
            translated.append(part)
    return "".join(translated)


def render_recipe_detail(recipe: dict, navigate: Callable[[str], None]) -> None:
    detail_context = st.session_state.get("selected_recipe_context", "library")
    back_label = "← Önerilere Dön" if detail_context == "personalized" else "← Tariflere Dön"
    if st.button(back_label):
        navigate("Sonuçlar" if detail_context == "personalized" else "Tarifler")
        st.rerun()
    if not recipe:
        st.info("Görüntülenecek tarif bulunamadı.")
        return
    local_match = next((item for item in get_local_recipes() if item.get("name") == recipe.get("name")), {})
    known_ingredients = recipe.get("ingredients") or local_match.get("ingredients", [])
    selected = {str(item).strip().lower() for item in st.session_state.get("ingredients", [])}
    missing = [item for item in known_ingredients if str(item).lower() not in selected] if known_ingredients else recipe.get("missing", [])
    show_missing = detail_context == "personalized" and bool(selected)

    left, right = st.columns([1.15, 1], gap="large")
    with left:
        primary_image, fallback_image = _recipe_image_sources(recipe)
        image_source = escape(primary_image, quote=True)
        fallback_source = escape(fallback_image, quote=True)
        st.markdown(
            f'<img class="detail-photo" src="{image_source}" '
            f'alt="{escape(str(recipe.get("name", "Tarif")))}" '
            f'onerror="this.onerror=null;this.src=\'{fallback_source}\';">',
            unsafe_allow_html=True,
        )
    with right:
        calories = float(recipe.get("calories", 0) or 0)
        protein = float(recipe.get("protein", 0) or 0)
        approximate = False
        carbohydrates = recipe.get("carbohydrates")
        fat = recipe.get("fat")
        if carbohydrates is None or fat is None:
            approximate = True
            fat = max(0, calories * 0.25 / 9)
            carbohydrates = max(0, (calories - protein * 4 - fat * 9) / 4)
        nutrition_label = " yaklaşık" if approximate else ""
        if show_missing:
            if missing:
                missing_tags = "".join(f'<span class="missing-tag">{escape(_clean_missing_ingredient(item))}</span>' for item in missing)
                missing_html = f'<div class="missing-panel" translate="no"><strong>Eksik malzemeler</strong>{missing_tags}</div>'
            else:
                missing_html = '<div class="complete-panel">✓ Bu tarif için eksik malzemen yok.</div>'
        else:
            missing_html = ""
        st.markdown(
            f'''<div class="detail-panel">
                <h1>{escape(str(recipe.get("name", "Tarif Detayı")))}</h1>
                <div class="detail-kicker">Porsiyon başına besin değerleri</div>
                <div class="nutrition-grid">
                    <div class="nutrition-box"><span>Kalori</span><strong>{calories:.0f} kcal</strong></div>
                    <div class="nutrition-box"><span>Protein</span><strong>{protein:.0f} g</strong></div>
                    <div class="nutrition-box"><span>Karbonhidrat{nutrition_label}</span><strong>{float(carbohydrates):.0f} g</strong></div>
                    <div class="nutrition-box"><span>Yağ{nutrition_label}</span><strong>{float(fat):.0f} g</strong></div>
                </div>
                {missing_html}
            </div>''',
            unsafe_allow_html=True,
        )

    st.divider()
    st.header("Tarif Rehberi")
    preparation_time, cooking_time = _recipe_times(recipe)
    time_one, time_two, serving = st.columns(3)
    time_one.metric("Hazırlık", f"{preparation_time} dk")
    time_two.metric("Pişirme", f"{cooking_time} dk")
    serving_count = int(recipe.get("servings") or 2)
    serving.metric("Porsiyon", f"{serving_count} kişilik")

    ingredients_area, instructions_area = st.columns([0.8, 1.4], gap="large")
    with ingredients_area:
        st.subheader(f"Malzemeler ({serving_count} kişilik)")
        ingredient_details = recipe.get("ingredient_details") or []
        if ingredient_details:
            for item in ingredient_details:
                st.markdown(f"- {item}")
        elif known_ingredients:
            for item in known_ingredients:
                st.markdown(f"- {_suggested_ingredient_amount(str(item))}")
            st.caption("Ölçüler 2 kişilik yerel tarif için önerilen yaklaşık miktarlardır.")
        else:
            st.info("Bu tarifin ölçülü malzeme listesi bulunamadı.")

    with instructions_area:
        st.subheader("Adım Adım Hazırlanışı")
        analyzed_steps = recipe.get("analyzed_steps") or []
        instructions = recipe.get("instructions")
        if analyzed_steps:
            for index, step in enumerate(analyzed_steps, 1):
                st.markdown(f"### {index}. adım")
                st.write(step.get("step", ""))
                step_ingredients = step.get("ingredients") or []
                equipment = step.get("equipment") or []
                if step_ingredients:
                    st.caption("Bu adımda: " + ", ".join(step_ingredients))
                if equipment:
                    st.caption("Gerekli ekipman: " + ", ".join(equipment))
        elif instructions:
            st.markdown(_translate_instruction_html(instructions), unsafe_allow_html=True)
        else:
            for index, step in enumerate(_detailed_local_instructions(recipe), 1):
                st.markdown(f"**{index}. adım —** {step}")
        st.info("İpucu: Pişirme süresi ocak ve malzeme kalınlığına göre değişebilir; yemeği kontrollü pişirin.")

    if recipe.get("source_url"):
        st.link_button("Orijinal tarifi aç →", recipe["source_url"], type="primary")


    st.divider()
    _render_inline_recipe_assistant(recipe, missing, show_missing)


def _render_inline_recipe_assistant(recipe: dict, missing: list, show_missing: bool) -> None:
    recipe_identity = str(recipe.get("id") or recipe.get("name", "tarif"))
    recipe_chat_key = "recipe_chat_" + hashlib.sha1(recipe_identity.encode("utf-8")).hexdigest()[:12]
    if recipe_chat_key not in st.session_state:
        st.session_state[recipe_chat_key] = []
    recipe_messages = st.session_state[recipe_chat_key]
    normalized_messages: list[dict] = []
    pending_user: dict | None = None
    for message in recipe_messages:
        role = message.get("role")
        content = str(message.get("content", "")).strip()
        if not content or role not in {"user", "assistant"}:
            continue
        if role == "user":
            if pending_user is None:
                pending_user = {"role": "user", "content": content}
            elif pending_user["content"] != content:
                # Cevapsız kalmış eski sorunun yerine en güncel soruyu tut.
                pending_user = {"role": "user", "content": content}
            continue
        if pending_user is not None:
            normalized_messages.extend([pending_user, {"role": "assistant", "content": content}])
            pending_user = None
        # Bir kullanıcı sorusuna bağlı olmayan fazladan asistan mesajını yok say.
    if normalized_messages != recipe_messages:
        st.session_state[recipe_chat_key] = normalized_messages
    recipe_messages = normalized_messages
    first_missing = _clean_missing_ingredient(missing[0]) if show_missing and missing else ""
    alternative_label = f"{first_missing.title()} alternatifi" if first_missing else "Malzeme alternatifi"
    alternative_question = (
        f"Bu tarifte {first_missing} yerine ne kullanabilirim?"
        if first_missing
        else "Bu tarifteki malzemeler için hangi alternatifleri kullanabilirim?"
    )

    with st.container(border=True):
        assistant_title, assistant_clear = st.columns([4, 1])
        with assistant_title:
            st.markdown(
                '<div class="inline-assistant-head"><div class="assistant-avatar">🌿</div>'
                '<div><h3>Bu Tarif İçin Asistana Sor</h3>'
                f'<p>{escape(str(recipe.get("name", "Bu tarif")))} hakkında sor; sayfadan ayrılmana gerek yok.</p></div></div>',
                unsafe_allow_html=True,
            )
        with assistant_clear:
            if st.button("Sohbeti Temizle", key=f"clear_{recipe_chat_key}", use_container_width=True):
                st.session_state[recipe_chat_key] = []
                st.rerun()

        if not recipe_messages:
            example_question = alternative_question.replace("Bu tarifte ", "")
            st.info(f"Örneğin “{example_question}” diye sorabilirsin.")
        for index in range(0, len(recipe_messages), 2):
            turn = recipe_messages[index:index + 2]
            with st.container(border=True):
                for message in turn:
                    with st.chat_message(message["role"]):
                        st.write(message["content"])

        quick_question = ""
        quick_columns = st.columns(3, gap="small")
        quick_options = [
            (alternative_label, alternative_question),
            ("Kaloriyi azalt", "Bu tarifi daha düşük kalorili hale nasıl getirebilirim?"),
            ("Proteini artır", "Bu tarifin proteinini nasıl artırabilirim?"),
        ]
        for column, (label, question) in zip(quick_columns, quick_options):
            if column.button(label, key=f"{recipe_chat_key}_{label}", use_container_width=True):
                quick_question = question

        with st.form(f"form_{recipe_chat_key}", clear_on_submit=True):
            detail_prompt = st.text_input(
                "Tarif hakkında sorun",
                placeholder=f"Örn: {alternative_question}",
                label_visibility="collapsed",
            )
            detail_submitted = st.form_submit_button("Asistana Sor ➤", type="primary", use_container_width=True)

        prompt_to_send = quick_question or (detail_prompt.strip() if detail_submitted else "")
        if prompt_to_send:
            duplicate_pending_question = (
                recipe_messages
                and recipe_messages[-1].get("role") == "user"
                and str(recipe_messages[-1].get("content", "")).strip() == prompt_to_send
            )
            if duplicate_pending_question:
                st.rerun()
            recipe_messages.append({"role": "user", "content": prompt_to_send})
            current_goal = st.session_state.get("goal", "Dengeli Beslenme")
            current_ingredients = st.session_state.get("ingredients", [])
            ai_answer = ask_gemini(prompt_to_send, [recipe], current_goal, current_ingredients)
            answer = ai_answer or _assistant_reply(prompt_to_send, [recipe], current_goal, current_ingredients)
            recipe_messages.append({"role": "assistant", "content": answer})
            st.session_state[recipe_chat_key] = recipe_messages
            st.rerun()


def render_about() -> None:
    st.markdown(
        f'''<section class="about-page">
            <div class="about-top">
                <div>
                    <p class="about-kicker">NUTRIMATCH HAKKINDA</p>
                    <h1 class="about-title">Beslenmeyi daha sade ve anlaşılır kılmak için</h1>
                    <p class="about-copy">NutriMatch, günlük beslenme kararlarını kolaylaştırma fikrinden doğdu. Ne pişireceğini bulmaktan yediğini anlamaya kadar uzanan süreci tek bir sade deneyimde buluşturur.</p>
                    <p class="about-copy">Amacımız kusursuz beslenme vaat etmek değil; kullanıcının elindeki bilgilerle daha bilinçli seçimler yapmasına yardımcı olmaktır.</p>
                </div>
                <img class="about-image" src="{ABOUT_IMAGE}" alt="Sağlıklı yemek tabağı">
            </div>
            <div class="about-quote">
                <span>Küçük seçimleri görünür, günlük takibi daha anlamlı hâle getiriyoruz.</span>
            </div>
            <div class="about-values">
                <div class="about-value"><span class="about-value-icon">◌</span><h3>Sadelik</h3><p>Karmaşık bilgileri anlaşılır sunarız.</p></div>
                <div class="about-value"><span class="about-value-icon">♧</span><h3>Kişiselleştirme</h3><p>Farklı hedeflere aynı kalıpla yaklaşmayız.</p></div>
                <div class="about-value"><span class="about-value-icon">◇</span><h3>Sorumluluk</h3><p>Sonuçları tahmin ve bilgilendirme olarak açıklarız.</p></div>
            </div>
            <div class="about-note"><span>ⓘ</span>NutriMatch profesyonel sağlık danışmanlığının yerini almaz.</div>
        </section>''',
        unsafe_allow_html=True,
    )


def render_assistant(
    recipes: list[dict],
    goal: str,
    ingredients: list[str],
    navigate: Callable[[str], None],
) -> None:
    if st.button("← Önerilere Dön", key="assistant_back"):
        navigate("Sonuçlar")
        st.rerun()
    st.markdown('<div class="assistant-header"><div class="assistant-avatar">🌿</div><div><h1>NutriMatch Tarif Asistanı</h1><div class="assistant-online">● Çevrimiçi · Tarifin için hazır</div></div></div>', unsafe_allow_html=True)
    st.markdown('<p class="muted">Tarifini geliştirebilir, alternatif malzeme bulabilir ve beslenme hedefine uygun değişiklikler sorabilirsin.</p>', unsafe_allow_html=True)
    if not recipes:
        st.info("Asistanı kullanmadan önce malzemelerinle tarif aramalısın.")
        return

    first = recipes[0]
    left, right = st.columns([1.05, 2.65], gap="large")
    with left:
        image = _image_source(str(first.get("image") or ""))
        image_html = f'<img class="assistant-recipe-image" src="{escape(image)}" alt="{escape(str(first.get("name", "Tarif")))}">' if image else ""
        st.markdown(
            f'<div class="assistant-side-card"><h3>🍽️ Seçili Tarif</h3>{image_html}'
            f'<div class="assistant-recipe-name">{escape(str(first.get("name", "Seçili tarif")))}</div>'
            f'<div class="assistant-mini-meta">🔥 {float(first.get("calories", 0) or 0):.0f} kcal &nbsp; · &nbsp; 💪 {float(first.get("protein", 0) or 0):.0f} g protein</div></div>',
            unsafe_allow_html=True,
        )
        ingredient_rows = "".join(
            f'<span><b class="assistant-check">✓</b>{escape(str(item).title())}</span>'
            for item in ingredients[:8]
        ) or '<span>Henüz malzeme seçilmedi.</span>'
        st.markdown(f'<div class="assistant-side-card" translate="no"><h3>🥬 Malzemelerin</h3><div class="assistant-list">{ingredient_rows}</div></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="assistant-side-card" translate="no"><h3>🎯 Hedefin</h3><div class="assistant-goal">{escape(goal or "Dengeli Beslenme")}</div></div>', unsafe_allow_html=True)

    with right:
        intro, clear = st.columns([3, 1])
        with intro:
            st.markdown('<div class="assistant-intro"><b>Merhaba! 👋</b><br>Seçtiğin tarif hakkında bana dilediğini sorabilirsin.</div>', unsafe_allow_html=True)
        with clear:
            if st.button("Sohbeti Temizle", use_container_width=True):
                st.session_state.messages = []
                st.rerun()

        if not st.session_state.messages:
            with st.chat_message("assistant"):
                st.write(f"{first.get('name', 'Bu tarif')} için alternatif malzeme, kalori azaltma veya besin değeri konusunda yardımcı olabilirim.")
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.write(message["content"])

        st.markdown('<div class="assistant-quick-label">Hızlı sorular</div>', unsafe_allow_html=True)
        quick_columns = st.columns(3, gap="small")
        quick_prompts = [
            ("Alternatif malzeme", "Bu tarifteki eksik malzemeler yerine ne kullanabilirim?"),
            ("Kaloriyi azalt", "Bu tarifi daha düşük kalorili yapmak için ne önerirsin?"),
            ("Tarifleri karşılaştır", "İlk iki tarifi kalori ve protein açısından karşılaştırır mısın?"),
        ]
        quick_prompt = ""
        for column, (label, question) in zip(quick_columns, quick_prompts):
            if column.button(label, key=f"assistant_quick_{label}", use_container_width=True):
                quick_prompt = question

        with st.form("assistant_form", clear_on_submit=True):
            prompt = st.text_input("Mesajın", placeholder="Bir şey sor…", label_visibility="collapsed")
            submitted = st.form_submit_button("Gönder ➤", type="primary", use_container_width=True)

    prompt_to_send = quick_prompt or (prompt.strip() if submitted else "")
    if prompt_to_send:
        st.session_state.messages.append({"role": "user", "content": prompt_to_send})
        ai_answer = ask_gemini(prompt_to_send, recipes, goal, ingredients)
        answer = ai_answer or _assistant_reply(prompt_to_send, recipes, goal, ingredients)
        st.session_state.messages.append({"role": "assistant", "content": answer})
        st.rerun()


def _assistant_reply(prompt: str, recipes: list[dict], goal: str, ingredients: list[str]) -> str:
    text = prompt.lower()
    first = recipes[0]
    missing = first.get("missing", [])
    knowledge = get_chatbot_knowledge()
    alternatives = knowledge.get("alternatives", {})
    for ingredient, replacement in alternatives.items():
        if ingredient in text and ("yerine" in text or "yok" in text or "alternatif" in text):
            answer = f"{ingredient.title()} yerine {replacement} kullanabilirsin."
            if "kalori" in text or "protein" in text:
                answer += f" {first.get('name', 'Bu tarif')} yaklaşık {first.get('calories', 0):.0f} kcal ve {first.get('protein', 0):.0f} g protein içeriyor."
            return answer
    for item in missing:
        if item.lower() in alternatives and ("alternatif" in text or "yerine" in text or "yok" in text):
            return f"{item} yerine {alternatives[item.lower()]} kullanabilirsin."
    if "karbonhidrat" in text or "yağ" in text or "yag" in text:
        carbohydrates = first.get("carbohydrates")
        fat = first.get("fat")
        if carbohydrates is None or fat is None:
            calories = float(first.get("calories", 0) or 0)
            protein = float(first.get("protein", 0) or 0)
            fat = calories * 0.25 / 9
            carbohydrates = max(0, (calories - protein * 4 - fat * 9) / 4)
        return f"{first.get('name', 'Bu tarif')} yaklaşık {float(carbohydrates):.0f} g karbonhidrat ve {float(fat):.0f} g yağ içeriyor."
    if "kalori" in text or "protein" in text or "besin" in text:
        return f"{first.get('name', 'Bu tarif')} yaklaşık {first.get('calories', 0):.0f} kcal ve {first.get('protein', 0):.0f} g protein içeriyor. Hedefin: {goal}."
    if "uygun" in text or "hedef" in text or "kas" in text or "kilo" in text:
        calories = float(first.get("calories", 0) or 0)
        protein = float(first.get("protein", 0) or 0)
        if goal == "Kas Yapma":
            verdict = "Protein miktarı iyi olduğu için kas yapma hedefine uygun görünüyor." if protein >= 20 else "Kas yapma hedefi için yanına protein içeren bir ekleme yapabilirsin."
        elif goal == "Kilo Verme":
            verdict = "Kalorisi görece düşük olduğu için kilo verme hedefine uygun görünüyor." if calories <= 400 else "Kilo verme hedefinde porsiyonu kontrollü tüketmen iyi olur."
        else:
            verdict = "Kalori ve protein dengesi açısından dengeli beslenme hedefinle uyumlu."
        return verdict
    for keyword, tip in knowledge.get("tips", {}).items():
        if keyword in text or ("porsiyon" in text and keyword == goal.lower()):
            return tip
    if "malzeme" in text or "eksik" in text:
        return "Elindeki malzemeler: " + (", ".join(ingredients) if ingredients else "belirtilmedi") + ". Eksikler: " + (", ".join(missing) if missing else "yok") + "."
    return f"Sana {len(recipes)} uygun tarif buldum. İlk önerim: {first.get('name', 'ilk tarif')}."
