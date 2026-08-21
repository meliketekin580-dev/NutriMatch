import streamlit as st

from services.recipe_service import (
    RecipeServiceError,
    get_popular_healthy_recipes,
    rank_recipe_candidates,
    save_recipes_to_local,
    search_local_recipes,
    search_recipes,
)
from ui import (
    apply_styles,
    render_about,
    render_assistant,
    render_discover,
    render_home,
    render_navigation,
    render_recipe_results,
    render_recipe_detail,
    render_recipes,
    get_local_recipes,
)


st.set_page_config(
    page_title="NutriMatch",
    page_icon="🥗",
    layout="wide",
    initial_sidebar_state="collapsed",
)

apply_styles()

if "page" not in st.session_state:
    st.session_state.page = "Ana Sayfa"
if "ingredients" not in st.session_state:
    st.session_state.ingredients = []
if "goal" not in st.session_state:
    st.session_state.goal = "Dengeli Beslenme"
if "recipes" not in st.session_state:
    st.session_state.recipes = []
if "messages" not in st.session_state:
    st.session_state.messages = []
if "popular_recipes" not in st.session_state:
    st.session_state.popular_recipes = []
if "selected_recipe" not in st.session_state:
    st.session_state.selected_recipe = {}


def go_to(page: str) -> None:
    st.session_state.page = page


def find_recipes(ingredients: list[str], goal: str) -> None:
    st.session_state.ingredients = ingredients
    st.session_state.goal = goal
    st.session_state.selected_recipe = {}
    local_matches = search_local_recipes(ingredients, goal)
    if len(local_matches) >= 3:
        st.session_state.recipes = rank_recipe_candidates(local_matches, goal)
        st.session_state.page = "Sonuçlar"
        return
    try:
        with st.spinner("Sana uygun tarifler aranıyor..."):
            api_matches = search_recipes(ingredients, goal)
            if api_matches:
                save_recipes_to_local(api_matches)
            st.session_state.recipes = rank_recipe_candidates(local_matches + api_matches, goal)
        st.session_state.page = "Sonuçlar"
    except RecipeServiceError as error:
        st.session_state.recipes = rank_recipe_candidates(local_matches, goal)
        st.session_state.page = "Sonuçlar"
        if not local_matches:
            st.warning("API kotası dolu; şu an bu malzemelerle yerel eşleşme bulunamadı.")


def load_popular_recipes() -> list[dict]:
    if st.session_state.popular_recipes:
        return st.session_state.popular_recipes
    try:
        st.session_state.popular_recipes = get_popular_healthy_recipes()
    except RecipeServiceError:
        st.session_state.popular_recipes = []
    return st.session_state.popular_recipes


render_navigation(st.session_state.page, go_to)

page = st.session_state.page
if page == "Ana Sayfa":
    render_home(go_to, load_popular_recipes())
elif page == "Tarifler":
    render_recipes(get_local_recipes(), go_to)
elif page == "Tarif Detayı":
    render_recipe_detail(st.session_state.selected_recipe, go_to)
elif page == "Hakkımızda":
    render_about()
elif page == "Tarifimi Keşfet":
    render_discover(find_recipes, go_to)
elif page == "Sonuçlar":
    render_recipe_results(st.session_state.recipes, st.session_state.goal, go_to)
elif page == "Asistan":
    render_assistant(
        st.session_state.recipes,
        st.session_state.goal,
        st.session_state.ingredients,
        go_to,
    )
