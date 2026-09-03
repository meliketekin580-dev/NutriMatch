import logging

import streamlit as st

from services.daily_meal_store import DailyMealStoreError, load_daily_meals, save_daily_meal
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

logger = logging.getLogger(__name__)


# Streamlit sayfasının tarayıcı başlığı, ikonu ve genel yerleşimi ayarlanır.
st.set_page_config(
    page_title="NutriMatch",
    page_icon="🥗",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Tüm sayfalarda kullanılan ortak görünüm kuralları uygulanır.
apply_styles()


def require_google_login() -> None:
    """Google OIDC oturumunu denetler ve giriş yapılmadıysa uygulamayı durdurur.

    Yapılandırmayı yalnızca ``.streamlit/secrets.toml`` içindeki ``[auth]``
    bölümünden alan Streamlit'in yerleşik giriş sistemini kullanır. Kullanıcı
    giriş yaptıysa uygulamanın devam etmesine izin verir; kimlik tokenları
    okunmaz, arayüze yazılmaz ve loglanmaz. Kullanıcı profili daha sonra
    ortak navigasyon alanında gösterilir.

    Returns:
        None: Giriş yapılmadığında ``st.stop()`` ile sayfanın kalanını durdurur.
    """
    st.markdown(
        """
        <style>
        .auth-welcome-card {
            max-width: 620px;
            margin: 8vh auto 1.25rem;
            padding: 2.4rem 2.2rem;
            text-align: center;
            border: 2px solid #cce7d5;
            border-radius: 24px;
            background: linear-gradient(135deg, #f0faf4 0%, #fff9ec 100%);
            box-shadow: 0 18px 42px rgba(13, 82, 50, 0.10);
        }
        .auth-welcome-logo {
            color: #168547;
            font-size: 2rem;
            font-weight: 800;
            margin-bottom: .8rem;
        }
        .auth-welcome-logo span { color: #f5a316; }
        .auth-welcome-card h1 {
            color: #061b3a;
            font-size: 2rem;
            margin: 0 0 .75rem;
        }
        .auth-welcome-card p {
            color: #52647a;
            font-size: 1.05rem;
            line-height: 1.65;
            margin: 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # OIDC ayarı yoksa giriş çağrısı hata vermeden önce anlaşılır bilgi gösterilir.
    auth_configured = "auth" in st.secrets
    if not auth_configured:
        st.markdown(
            """
            <div class="auth-welcome-card">
                <div class="auth-welcome-logo">Nutri<span>Match</span></div>
                <h1>Beslenme yolculuğuna hoş geldin</h1>
                <p>Tariflerini, öğün analizlerini ve günlük takibini güvenli biçimde kullanmak için Google hesabınla giriş yap.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.warning("Google ile giriş henüz yapılandırılmadı. `.streamlit/secrets.toml` dosyasına `[auth]` ayarlarını ekleyin.")
        st.button("Google ile Giriş Yap", type="primary", use_container_width=True, disabled=True)
        st.stop()

    # Kullanıcı giriş yapmadıysa yalnızca karşılama kartı ve giriş düğmesi gösterilir.
    if not st.user.is_logged_in:
        st.markdown(
            """
            <div class="auth-welcome-card">
                <div class="auth-welcome-logo">Nutri<span>Match</span></div>
                <h1>Beslenme yolculuğuna hoş geldin</h1>
                <p>Tariflerini, öğün analizlerini ve günlük takibini güvenli biçimde kullanmak için Google hesabınla giriş yap.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Google ile Giriş Yap", type="primary", use_container_width=True):
            st.login()
        st.stop()

    # Kullanıcı giriş yaptıysa profil ve çıkış denetimi ortak navbar içinde gösterilir.


# Kimlik doğrulama tamamlanmadan oturum verileri, MongoDB veya ana arayüz yüklenmez.
require_google_login()

# Oturum ilk kez açıldığında sayfa ve kullanıcı verileri için başlangıç değerleri oluşturulur.
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
# Sağlıklı vitrin kuralları güncellendiğinde eski, zayıf filtrelenmiş API
# sonuçlarının oturumda kalmamasını sağlar.
if st.session_state.get("popular_recipes_version") != 2:
    st.session_state.popular_recipes = []
    st.session_state.popular_recipes_version = 2
if "selected_recipe" not in st.session_state:
    st.session_state.selected_recipe = {}
# Aynı tarayıcı oturumunda farklı Google hesabına geçilirse önceki hesabın
# bellekteki öğünleri yeni hesaba taşınmaz; liste MongoDB'den yeniden yüklenir.
active_daily_meals_user_id = str(st.user.get("sub") or "").strip()
if st.session_state.get("daily_meals_user_id") != active_daily_meals_user_id:
    st.session_state.pop("daily_meals", None)
    st.session_state.daily_meals_store_synced = False
    st.session_state.daily_meals_user_id = active_daily_meals_user_id
if "daily_meals" not in st.session_state:
    try:
        # Daha önce SQLite'a kaydedilen günlük öğünler oturuma alınır.
        st.session_state.daily_meals = load_daily_meals()
    except DailyMealStoreError as exc:
        logger.warning("Günlük öğünler başlangıçta yüklenemedi: %s", exc)
        # Kalıcı kayıt okunamazsa uygulama boş bellek listesiyle çalışmaya devam eder.
        st.session_state.daily_meals = []
if not st.session_state.get("daily_meals_store_synced"):
    try:
        # Bellekte olup kalıcı kayıtta bulunmayan analizli öğünler SQLite'a yazılır.
        for daily_meal in st.session_state.daily_meals:
            if isinstance(daily_meal, dict) and daily_meal.get("analysis_id"):
                save_daily_meal(daily_meal)
        st.session_state.daily_meals_store_synced = True
    except DailyMealStoreError as exc:
        logger.warning("Oturumdaki günlük öğünler MongoDB ile eşitlenemedi: %s", exc)
        # Uygulama günlük özelliğini bellek üzerinden kullanmaya devam eder.
        pass
if "daily_completion_cache" not in st.session_state:
    st.session_state.daily_completion_cache = {}


def go_to(page: str) -> None:
    """Uygulamanın gösterileceği sayfayı oturum içinde değiştirir.

    Args:
        page: Gösterilmesi istenen sayfanın adı.

    Returns:
        None: Sayfa adı doğrudan ``st.session_state`` içine kaydedilir.
    """
    # Navigasyon bileşenleri bu değer üzerinden hangi ekranın çizileceğini belirler.
    st.session_state.page = page


def find_recipes(ingredients: list[str], goal: str) -> None:
    """Malzemeler ve hedefe göre tarif adaylarını bulur, sıralar ve sonuç sayfasına geçer.

    Args:
        ingredients: Kullanıcının seçtiği veya yazdığı malzeme adları.
        goal: Kullanıcının seçtiği beslenme hedefi.

    Returns:
        None: Bulunan tarifler ve seçimler oturum durumuna kaydedilir.
    """
    # Kullanıcının güncel seçimleri sonraki sayfalarda kullanılmak üzere saklanır.
    st.session_state.ingredients = ingredients
    st.session_state.goal = goal
    st.session_state.selected_recipe = {}
    # Önce kota tüketmeyen yerel tarif verileri içinde eşleşme aranır.
    local_matches = search_local_recipes(ingredients, goal)
    if len(local_matches) >= 3:
        # Yeterli yerel sonuç varsa dış API'ye istek göndermeden sonuçlar sıralanır.
        st.session_state.recipes = rank_recipe_candidates(local_matches, goal)
        st.session_state.page = "Sonuçlar"
        return
    try:
        with st.spinner("Sana uygun tarifler aranıyor..."):
            # Yerel sonuç az olduğunda tarif servisi üzerinden ek adaylar alınır.
            api_matches = search_recipes(ingredients, goal)
            if api_matches:
                # API'den gelen tarifler sonraki aramalarda kullanılmak üzere yerelde saklanır.
                save_recipes_to_local(api_matches)
            # Yerel ve API sonuçları birlikte hedefe göre sıralanır.
            st.session_state.recipes = rank_recipe_candidates(local_matches + api_matches, goal)
        st.session_state.page = "Sonuçlar"
    except RecipeServiceError as error:
        # Servis hatasında varsa yalnızca yerel sonuçlar gösterilir.
        st.session_state.recipes = rank_recipe_candidates(local_matches, goal)
        st.session_state.page = "Sonuçlar"
        if not local_matches:
            st.warning("API kotası dolu; şu an bu malzemelerle yerel eşleşme bulunamadı.")


def load_popular_recipes() -> list[dict]:
    """Ana sayfadaki öne çıkan sağlıklı tarifleri oturum önbelleğiyle yükler.

    Args:
        Bu fonksiyon dışarıdan değer almaz.

    Returns:
        list[dict]: Ana sayfada gösterilecek tarif sözlükleri.
    """
    if st.session_state.popular_recipes:
        # Daha önce yüklenen liste varsa yeni servis isteği yapılmaz.
        return st.session_state.popular_recipes
    try:
        # Öne çıkan tarifler tarif servisinden alınarak oturuma kaydedilir.
        st.session_state.popular_recipes = get_popular_healthy_recipes()
    except RecipeServiceError:
        # Servis kullanılamazsa ana sayfa boş bir tarif listesiyle çizilir.
        st.session_state.popular_recipes = []
    return st.session_state.popular_recipes


# Üst navigasyon, aktif sayfa bilgisi ve sayfa değiştirme işleviyle çizilir.
render_navigation(st.session_state.page, go_to)

# Oturumda seçilen sayfaya göre yalnızca ilgili arayüz bölümü gösterilir.
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
