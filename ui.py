from html import escape
from base64 import b64encode
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import streamlit as st

from services.ai_service import analyze_food_image, ask_gemini
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


@st.cache_data(show_spinner=False)
def _image_source(source: str) -> str:
    """Yerel görselleri HTML kartlarında kullanılabilen data URL'ye çevir."""
    if source.startswith("static/"):
        path = Path(__file__).parent / source
        try:
            encoded = b64encode(path.read_bytes()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except OSError:
            return ""
    return source


def get_local_recipes() -> list[dict]:
    catalog_path = Path(__file__).parent / "data" / "recipes.json"
    extra_catalog_path = Path(__file__).parent / "data" / "recipes_extra.json"
    ingredients_path = Path(__file__).parent / "data" / "recipe_ingredients.json"
    extra_ingredients_path = Path(__file__).parent / "data" / "recipe_ingredients_extra.json"
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
            with extra_ingredients_path.open(encoding="utf-8") as file:
                ingredients.update(json.load(file))
        except (OSError, ValueError):
            pass
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
            if recipe.get("name") in image_overrides:
                recipe["image"] = image_overrides[recipe["name"]]
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
        h1, h2, h3 {color:#071a38 !important; letter-spacing:0;}
        [data-testid="stHorizontalBlock"]:has(.home-hero-copy) {height:520px; min-height:520px; align-items:stretch; gap:0; margin:2px 0 38px; padding:0; overflow:hidden; border:1px solid #0b5a3d; border-radius:22px; background:#034d34 center/cover no-repeat; box-shadow:0 14px 36px rgba(15,23,42,.12);}
        [data-testid="stHorizontalBlock"]:has(.home-hero-copy) > [data-testid="stColumn"]:first-child {display:flex; flex-direction:column; justify-content:center; padding:38px 38px 34px 46px; background:linear-gradient(90deg,rgba(0,55,37,.98),rgba(0,79,51,.91) 70%,rgba(0,79,51,0));}
        [data-testid="stHorizontalBlock"]:has(.home-hero-copy) > [data-testid="stColumn"]:last-child {min-height:0; padding:0;}
        .home-hero-copy {display:block; width:1px; height:1px; overflow:hidden; opacity:0; pointer-events:none;}
        .eyebrow {display:inline-flex; width:fit-content; align-items:center; padding:8px 13px; border-radius:999px; background:rgba(34,197,94,.17); border:1px solid rgba(134,239,172,.22); color:#f0fff5; font-size:14px; font-weight:800; margin-bottom:10px;}
        .hero-title {font-size:43px; line-height:1.08; font-weight:850; color:#fff; margin:14px 0 17px; letter-spacing:-1px;}
        .hero-title span {color:#55d56b;}
        .muted {color:#526779; line-height:1.7;}
        .hero-title + .muted {max-width:520px; color:#eef9f2 !important; font-size:16px; line-height:1.6; font-weight:600; margin:0 0 16px;}
        .hero-image-frame {display:none;}
        .hero-image-frame .hero-image {display:block; width:100% !important; max-width:none !important; height:100% !important; object-fit:cover; object-position:center; border-radius:0 21px 21px 0; box-shadow:none;}
        .hero-image {display:block; width:100% !important; max-width:none !important; height:405px; object-fit:cover; object-position:center; border-radius:22px; box-shadow:0 16px 35px rgba(15,23,42,.13);}
        .hero-proof {display:flex; flex-wrap:wrap; gap:10px; margin:20px 0 20px;}
        .hero-proof span {display:inline-flex; align-items:center; gap:7px; min-height:42px; padding:9px 13px; border:1.5px solid #9ac8aa; border-radius:12px; color:#183f2d; font-size:14px; font-weight:780; box-shadow:0 5px 12px rgba(31,122,67,.08);}
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
        .home-steps-heading {text-align:left; margin:4px 0 0; padding:18px 8px 0 0;}
        .home-steps-heading h2 {margin:0 0 14px; font-size:27px; font-weight:850;}
        .home-steps-heading p {margin:0; max-width:190px; color:#526779; font-size:15px; line-height:1.6;}
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
        .recipe-card img {width:100%; height:235px; object-fit:cover; display:block;}
        .home-recipe-card {display:grid; grid-template-columns:52% 48%; min-height:255px; height:255px;}
        .home-recipe-card img,.home-recipe-card .recipe-image-placeholder {width:100%; height:255px; min-height:255px;}
        .home-recipe-card .recipe-body {display:flex; flex-direction:column; justify-content:center; min-width:0; padding:16px 15px;}
        .home-recipe-card .recipe-name {min-height:auto; font-size:16px; margin-bottom:8px;}
        .home-recipe-card .recipe-meta {gap:7px; margin-top:8px;}
        .home-recipe-card .goal-pill {margin:8px 0 0; width:fit-content;}
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
        .discover-intro {margin:2px 0 24px; padding:20px 23px; border-left:6px solid #1f7a43; border-radius:15px; background:linear-gradient(120deg,#edf8f0,#fff8e8); box-shadow:0 7px 18px rgba(31,122,67,.08);}
        .discover-intro h1 {margin:0 0 7px; font-size:38px; line-height:1.15; font-weight:850;}
        .discover-intro p {margin:0; color:#425d4d; font-size:16px; line-height:1.6; font-weight:550;}
        .discover-intro + .muted {display:none;}
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
        [data-testid="stTabs"] [data-baseweb="tab"] {height:50px; padding:0 18px; border:1px solid #d8e4dc; border-bottom:0; border-radius:11px 11px 0 0; background:#fffdf8; color:#263d32; font-size:15px; font-weight:750; white-space:nowrap;}
        [data-testid="stTabs"] [data-baseweb="tab"] p {font-size:15px; font-weight:750;}
        [data-testid="stTabs"] [aria-selected="true"] {border-color:#83bd96; background:#eaf7ee; color:#14532d; box-shadow:inset 0 -3px 0 #1f7a43;}
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
            display:grid !important; grid-template-columns:repeat(4,minmax(0,1fr)) !important;
            gap:14px !important; padding:10px !important; border:2px solid #d7e8dc !important;
            border-radius:16px !important; background:#f6faf7 !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] label {
            display:flex !important; justify-content:center !important; min-height:72px !important;
            padding:14px 16px !important; border:2px solid #d3e0d7 !important;
            border-radius:13px !important; background:#fff !important; text-align:center !important;
            box-shadow:0 3px 9px rgba(15,23,42,.05) !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] label p {
            color:#10233f !important; font-size:19px !important; line-height:1.25 !important;
            font-weight:850 !important; white-space:nowrap !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] label:has(input:checked) {
            border-color:#1f7a43 !important; background:#e4f5e9 !important;
            box-shadow:inset 0 -5px 0 #1f7a43,0 5px 12px rgba(31,122,67,.13) !important;
        }
        .st-key-discover_section_nav [data-testid="stRadio"] label:has(input:checked) p {color:#0c6634 !important;}
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
            .st-key-discover_section_nav [data-testid="stRadio"] > div[role="radiogroup"]{grid-template-columns:1fr !important;}
            .st-key-discover_section_nav [data-testid="stRadio"] label p{font-size:17px !important;}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_navigation(current: str, navigate: Callable[[str], None]) -> None:
    columns = st.columns([2.3, 1, 1, 1, 2.1], gap="small")
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
        if st.button("Tarifimi Keşfet +", key="nav_discover", type="primary", use_container_width=True):
            navigate("Tarifimi Keşfet")
            st.rerun()

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
        st.markdown('<div class="eyebrow">Akıllı Tarif Asistanı</div>', unsafe_allow_html=True)
        st.markdown('<div class="hero-title">Evindeki özelliklerle<br><span>harika tarifler keşfedin!</span></div>', unsafe_allow_html=True)
        st.markdown('<p class="muted">Elindeki malzemeleri gir, sana en uygun sağlıklı tarifleri saniyeler içinde bulalım.</p>', unsafe_allow_html=True)
        st.markdown(
            '<div class="hero-proof"><span><b>∞</b> Sınırsız tarif keşfi</span><span><b>3</b> Kişisel hedef</span><span><b>AI</b> Akıllı asistan</span></div>',
            unsafe_allow_html=True,
        )
        if st.button("Tariflere Göz At →", key="hero_browse", type="primary"):
            navigate("Tarifler")
            st.rerun()

    with right:
        st.markdown('<span class="home-hero-visual" aria-hidden="true"></span>', unsafe_allow_html=True)

    cards = [
        ("01", '<svg viewBox="0 0 32 32" aria-hidden="true"><rect x="8" y="5" width="16" height="22" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 14h16M12 9h4M12 19h8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>', "Malzemeleri Seç", "Evinde bulunan malzemeleri seç veya yaz."),
        ("02", '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="m18 5 2 6 6 2-6 2-2 6-2-6-6-2 6-2 2-6ZM9 20l1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>', "Tarifleri Keşfet", "Sana en uygun sağlıklı tarifleri bulalım."),
        ("03", '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M7 18h18a9 9 0 0 1-18 0Z" fill="none" stroke="currentColor" stroke-width="2"/><path d="M16 11c-3-5 5-5 2-9M11 14c-2-3 3-4 1-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M16 22c-2-3-7-1-7-5 0-3 4-4 7-1 3-3 7-2 7 1 0 4-5 2-7 5Z" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>', "Afiyetle Ye", "Lezzetli ve sağlıklı tariflerin tadını çıkar!"),
    ]
    recipes = (popular_recipes or FEATURED_RECIPES)[:3]

    step_columns = st.columns([0.72, 1.16, 1.16, 1.16], gap="large")
    with step_columns[0]:
        st.markdown(
            '<div class="home-steps-heading"><h2>Nasıl Çalışır?</h2><p>Sadece 3 adımda sana özel tariflere ulaş.</p></div>',
            unsafe_allow_html=True,
        )
    for column, (number, icon, title, text) in zip(step_columns[1:], cards):
        with column:
            st.markdown(
                f'<div class="info-card home-step-card"><div class="step-number">{number}</div>'
                f'<div class="step-content"><div class="info-icon">{icon}</div><div><h4>{title}</h4>'
                f'<p class="muted">{text}</p></div></div></div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div class="section popular-heading"><h2>Popüler Sağlıklı Tarifler</h2></div>', unsafe_allow_html=True)
    _recipe_grid(recipes, "Dengeli Beslenme", navigate, card_variant="home")


def _format_label_value(value: object, unit: str) -> str:
    if value is None:
        return "Okunamadı"
    number = float(value)
    formatted = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{formatted} {unit}".strip()


def _render_label_analysis_result(result: dict, goal: str) -> None:
    product_name = escape(str(result.get("product_name") or "Ürün adı okunamadı"))
    basis_type = escape(str(result.get("basis_type") or "bilinmiyor"))
    score = result.get("match_score")
    score_text = f"{float(score):.0f}" if score is not None else "—"

    st.markdown(
        f'<div class="label-result-hero"><div><span>AI BESİN ETİKETİ RAPORU</span><h2>{product_name}</h2><p>Hedef: <b>{escape(goal)}</b> · Değerlerin ölçüsü: <b>{basis_type}</b></p></div><div class="label-score"><strong>{score_text}</strong><small>/ 100</small><em>NutriMatch Uygunluk Puanı</em></div></div>',
        unsafe_allow_html=True,
    )
    st.caption("Bu puan yaklaşık ve yalnızca genel bilgilendirme amaçlıdır.")

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
        st.markdown("### 🎯 Hedefine Göre AI Yorumu")
        st.write(result.get("goal_explanation") or "Bu fotoğraftan hedefe göre güvenilir bir yorum üretilemedi.")

    unreadable = result.get("unreadable_fields") or []
    if unreadable:
        st.warning("Etikette okunamayan bilgiler: " + ", ".join(str(item) for item in unreadable))
    st.info("Fotoğraftan okunan değerleri ürün ambalajındaki orijinal besin tablosuyla mutlaka karşılaştır.")
    st.markdown('<div class="label-disclaimer">Bu analiz genel bilgilendirme amaçlıdır. Yapay zekâ etiketi hatalı okuyabilir; değerleri ürün ambalajından doğrulayın. Bu sonuç tıbbi veya diyetetik tavsiye değildir.</div>', unsafe_allow_html=True)


def _render_nutrition_label_analysis() -> None:
    st.markdown('<div class="label-analysis-head"><div><h1>AI Besin Etiketi Analizi</h1><p>Ürünün besin değerleri tablosunun net bir fotoğrafını yükle; yapay zekâ etiketi okuyup seçtiğin hedefe göre değerlendirsin.</p></div></div>', unsafe_allow_html=True)
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
    uploaded_label = st.file_uploader(
        "Besin değerleri tablosunun fotoğrafı",
        type=["jpg", "jpeg", "png"],
        key="nutrition_label_uploader",
        help="En fazla 10 MB boyutunda, yazıları net görünen bir fotoğraf seç.",
    )
    if uploaded_label is None:
        st.markdown('<div class="label-upload-empty">📷 Etiketi mümkün olduğunca düz, aydınlık ve yakından çek.</div>', unsafe_allow_html=True)
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
        analyze_clicked = st.button("Etiketi AI ile Analiz Et", type="primary", key="analyze_nutrition_label", use_container_width=True)

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
    if st.button("+ Bu Öğünü Günüme Ekle", type="primary", key=f"add_daily_meal_{analysis_id}", use_container_width=True):
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
        if add_daily_meal(st.session_state.daily_meals, record):
            st.session_state.meal_added_message = "Öğün gününe eklendi."
        else:
            st.session_state.meal_added_message = "Bu öğün daha önce gününe eklendi."
    if st.session_state.get("meal_added_message"):
        st.success(st.session_state.meal_added_message)


def _render_meal_analysis() -> None:
    st.markdown('<div class="meal-analysis-head"><span>🍽️</span><div><h1>Tabağımı Analiz Et</h1><p>Tabağının fotoğrafını yükle, yaklaşık besin değerini hesaplayalım.</p></div></div>', unsafe_allow_html=True)
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
    uploaded_meal = st.file_uploader(
        "Tabağının fotoğrafı",
        type=["jpg", "jpeg", "png"],
        key="meal_photo_uploader",
        help="En fazla 10 MB boyutunda, tabağın tamamını net gösteren bir fotoğraf seç.",
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
            analyze_clicked = st.button("Tabağımı AI ile Analiz Et", type="primary", key="analyze_meal_photo", use_container_width=True)
    else:
        st.markdown('<div class="meal-upload-empty">📷 Tabağı üstten veya hafif çapraz açıyla, aydınlık ve net biçimde çek.</div>', unsafe_allow_html=True)
        analyze_clicked = st.button("Tabağımı AI ile Analiz Et", type="primary", key="analyze_meal_without_photo")

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


def render_discover(search: Callable[[list[str], str], None], navigate: Callable[[str], None]) -> None:
    if st.button("← Geri Dön", key="discover_back_home"):
        navigate("Ana Sayfa")
        st.rerun()
    section_options = ["🍲 Malzemelerimden Tarif Bul", "🏷️ Besin Etiketi Analizi", "🍽️ Tabağımı Analiz Et", "▶️ Hedefime Uygun Videolar"]
    with st.container(key="discover_section_nav"):
        active_section = st.radio(
            "Tarifimi Keşfet bölümü",
            section_options,
            horizontal=True,
            label_visibility="collapsed",
            key="discover_section",
        )
    if active_section == section_options[0]:
        _render_ingredient_discover(search, navigate)
    elif active_section == section_options[1]:
        _render_nutrition_label_analysis()
    elif active_section == section_options[2]:
        _render_meal_analysis()
    else:
        _render_workout_videos(st.session_state.get("goal", "Dengeli Beslenme"))


def _render_ingredient_discover(search: Callable[[list[str], str], None], navigate: Callable[[str], None]) -> None:

    st.markdown(
        '<div class="discover-intro"><h1>Malzemelerini Gir</h1>'
        '<p>Elindeki malzemeleri fotoğraftan veya elle ekle; hedefinle uyumlu tarifleri birlikte bulalım.</p></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<p class="muted">Elindeki malzemeleri ekleyerek sana uygun tarifleri bulmamıza yardımcı ol.</p>', unsafe_allow_html=True)

    if "image_analysis_cache" not in st.session_state:
        st.session_state.image_analysis_cache = {}

    with st.container(border=True):
        st.markdown(
            '<div class="photo-analysis-head"><h3><span class="photo-analysis-icon">✦</span>Fotoğraftan Malzemeleri Bul</h3>'
            '<p>Buzdolabının veya yiyeceklerinin fotoğrafını yükle; yapay zekâ gördüğü yenilebilir malzemeleri listene eklesin.</p></div>',
            unsafe_allow_html=True,
        )
        uploaded_photo = st.file_uploader(
            "Yiyecek fotoğrafı",
            type=["jpg", "jpeg", "png"],
            key="ingredient_photo_uploader",
            help="JPG, JPEG veya PNG biçiminde bir fotoğraf yükleyebilirsin.",
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
                    if cached_ingredients is None:
                        with st.spinner("Fotoğraftaki malzemeler tanınıyor..."):
                            cached_ingredients = analyze_food_image(photo_bytes, uploaded_photo.type)
                        if cached_ingredients is not None:
                            st.session_state.image_analysis_cache[photo_hash] = cached_ingredients
                    if cached_ingredients is None:
                        st.error("Fotoğraf şu anda analiz edilemedi. Gemini API anahtarını ve kotanı kontrol edip tekrar deneyebilirsin.")
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
    if st.button("Tarifleri Keşfet →", type="primary", use_container_width=True, disabled=not combined):
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
    image = escape(_image_source(str(recipe.get("image", ""))), quote=True)
    if image:
        image_html = f'<img src="{image}" alt="{name}">'
    else:
        image_html = '''<div class="recipe-image-placeholder"><svg viewBox="0 0 48 48" aria-hidden="true"><path d="M9 27h30a15 15 0 0 1-30 0Z" fill="none" stroke="currentColor" stroke-width="2.5"/><path d="M24 19c-4-7 6-7 3-14M17 21c-3-5 4-6 2-11" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg><span>Görsel hazırlanıyor</span></div>'''
    missing = recipe.get("missing", [])
    missing_text = ", ".join(escape(_clean_missing_ingredient(item)) for item in missing[:4]) if missing else "Eksik malzeme yok"
    label = escape(str(recipe.get("label", "Yerel tarif")))
    detail = escape(str(recipe.get("detail", f"{recipe.get('used', 0)}/{recipe.get('total', 0)} malzeme uyumu")))
    missing_html = f'<div class="missing" translate="no"><strong>Eksikler:</strong> {missing_text}</div>' if recipe.get("show_missing", True) and missing else ""
    score_html = f'<span class="goal-pill">{escape(goal)} · {recipe.get("score", 0)} puan</span>' if show_score else ""
    card_class = "recipe-card" if show_score else "recipe-card recipe-card--library"
    if card_variant == "home":
        card_class += " home-recipe-card"
    return f"""
    <div class="{card_class}">
      {image_html}
      <div class="recipe-body">
        <div class="recipe-type">{detail}</div>
        <div class="recipe-name">{name}</div>
        <div class="recipe-meta"><span class="nutrition"><span class="meta-icon">♨</span> {recipe.get('calories', 0):.0f} kcal</span><span class="nutrition"><span class="meta-icon">♧</span> {recipe.get('protein', 0):.0f} g protein</span>{score_html}</div>
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
                st.markdown(_recipe_card(recipe, goal, show_score=show_score, card_variant=card_variant), unsafe_allow_html=True)
                if navigate and st.button("Tarifi İncele", key=f"detail_{recipe.get('id', recipe.get('name', start))}", use_container_width=True):
                    st.session_state.selected_recipe = recipe
                    st.session_state.selected_recipe_context = detail_context
                    navigate("Tarif Detayı")
                    st.rerun()


def _render_workout_videos(goal: str) -> None:
    st.markdown('<div class="workout-section-head"><span>▶</span><div><h2>Hedefini Destekleyen Videolar</h2><p>Beslenme hedefini hareketle desteklemek için seviyeni seç.</p></div></div>', unsafe_allow_html=True)
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
    st.title("Tarif Kütüphanesi")
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
        image_source = escape(_image_source(str(recipe.get("image", ""))), quote=True)
        if image_source:
            st.markdown(f'<img class="detail-photo" src="{image_source}" alt="{escape(str(recipe.get("name", "Tarif")))}">', unsafe_allow_html=True)
        else:
            st.markdown('''<div class="detail-photo-placeholder"><svg viewBox="0 0 80 80" aria-hidden="true"><path d="M14 45h52a26 26 0 0 1-52 0Z" fill="none" stroke="currentColor" stroke-width="3"/><path d="M40 31c-7-11 10-12 5-23M28 35c-5-8 7-10 3-18" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg><span>Bu tarifin görseli hazırlanıyor</span></div>''', unsafe_allow_html=True)
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
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.title("Hakkımızda")
        st.markdown('<p class="muted">NutriMatch, evindeki malzemelerle sağlıklı ve lezzetli tarif keşfetmeni sağlayan akıllı bir tarif asistanıdır.</p>', unsafe_allow_html=True)
        st.markdown('<p class="muted">Amacımız, sağlıklı beslenmeyi kolaylaştırmak ve herkesin kendi mutfağında israf olmadan seçim yapmasına yardımcı olmaktır.</p>', unsafe_allow_html=True)
        st.markdown("Kişiselleştirilmiş Tarif Önerileri  \nSağlıklı ve Dengeli Beslenme  \nZaman Tasarrufu  \nYüzlerce Lezzetli Tarif")

    with right:
        st.markdown(f'<img class="hero-image" src="{ABOUT_IMAGE}" alt="Sağlıklı tabak">', unsafe_allow_html=True)

    st.markdown("<br><br>", unsafe_allow_html=True)
    columns = st.columns(3, gap="large")
    stats = [("10+", "Her aramada tarif"), ("3", "Beslenme hedefi"), ("100%", "Kişiselleştirilmiş")]
    for column, (value, label) in zip(columns, stats):
        column.markdown(f'<div class="stat-card"><strong>{value}</strong><span>{label}</span></div>', unsafe_allow_html=True)


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
