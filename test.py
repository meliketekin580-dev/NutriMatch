import unittest
import json
import re
from datetime import date, datetime
from unittest.mock import MagicMock, Mock, patch

import requests

from utils.scoring import calculate_recipe_score, is_recipe_suitable_for_goal
from services.recipe_service import (
    load_local_ingredients,
    merge_ingredient_maps,
    rank_recipe_candidates,
    search_local_recipes,
)
from services.nutrition_label_service import (
    NutritionLabelError,
    get_or_analyze_label,
    parse_label_response,
)
from services.ai_service import (
    GEMINI_AUTH_MESSAGE,
    GEMINI_DEFAULT_ERROR_MESSAGE,
    GEMINI_OVERLOADED_MESSAGE,
    GEMINI_QUOTA_MESSAGE,
    GEMINI_TIMEOUT_MESSAGE,
    gemini_error_message,
)
from services.meal_analysis_service import (
    MealAnalysisError,
    _raise_for_gemini_status,
    _response_schema as meal_response_schema,
    add_daily_meal,
    calculate_meal_totals,
    get_or_analyze_meal,
    parse_meal_response,
    reset_meal_state_for_new_image,
    scale_meal_items,
)
from services.translator import translate_to_turkish
from services.daily_meal_store import (
    _document_to_record,
    _migrate_sqlite_records_once,
    _mongo_failure_category,
    _public_mongo_error,
    _record_to_document,
    claim_legacy_daily_meals_for_user,
    count_claimable_legacy_daily_meals,
    delete_daily_meal,
    load_daily_meals,
    local_meal_date,
    meals_for_date,
    save_daily_meal,
    update_daily_meal,
)
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError


class RecipeScoringTests(unittest.TestCase):
    def test_single_ingredient_catalog_has_all_recipes_and_current_values(self):
        """Yerel malzeme kataloğunun beklenen tarifleri ve malzemeleri içerdiğini sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Yerel JSON verisinden tarif-malzeme eşlemesi okunur.
        ingredients = load_local_ingredients()
        self.assertEqual(len(ingredients), 30)
        self.assertEqual(ingredients["kakaolu protein smoothie"], ["kakao", "süt", "muz"])
        self.assertEqual(ingredients["ızgara tavuk salata"], ["tavuk", "salata", "domates", "salatalık"])

    def test_newer_ingredient_map_overrides_same_recipe(self):
        """Aynı tarif iki kaynakta olduğunda güncel malzeme listesinin kullanıldığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # İki malzeme haritası birleştirilerek çakışan tarifin sonucu doğrulanır.
        merged = merge_ingredient_maps(
            {"aynı tarif": ["eski malzeme"], "korunan tarif": ["yumurta"]},
            {"aynı tarif": ["güncel malzeme"]},
        )
        self.assertEqual(merged["aynı tarif"], ["güncel malzeme"])
        self.assertEqual(merged["korunan tarif"], ["yumurta"])

    def test_score_is_an_integer(self):
        """Tarif puanı hesaplamasının tam sayı döndürdüğünü sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        score = calculate_recipe_score(420, 28, 35, 14, 3, 5, "Dengeli Beslenme")
        self.assertIsInstance(score, int)

    def test_matching_ingredients_improve_score(self):
        """Daha çok eşleşen malzemenin tarif puanını artırdığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Aynı besin değerleriyle kısmi ve tam eşleşmenin puanları karşılaştırılır.
        partial = calculate_recipe_score(300, 20, 25, 10, 2, 5, "Dengeli Beslenme")
        complete = calculate_recipe_score(300, 20, 25, 10, 5, 5, "Dengeli Beslenme")
        self.assertGreater(complete, partial)

    def test_muscle_goal_rewards_more_protein(self):
        """Kas yapma hedefinde yüksek proteinin daha yüksek puan aldığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        lower = calculate_recipe_score(500, 10, 40, 15, 4, 5, "Kas Yapma")
        higher = calculate_recipe_score(500, 30, 40, 15, 4, 5, "Kas Yapma")
        self.assertGreater(higher, lower)

    def test_weight_loss_rejects_extreme_calorie_recipe(self):
        """Kilo verme hedefinde çok yüksek kalorili tariflerin uygun olmadığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        self.assertFalse(is_recipe_suitable_for_goal(1368, 69, "Kilo Verme"))
        self.assertTrue(is_recipe_suitable_for_goal(320, 8, "Kilo Verme"))

    def test_all_sources_use_same_ranking_and_goal_filter(self):
        """Tarif adaylarının hedef filtresi ve sıralama kurallarıyla işlendiğini sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Farklı uyum oranları, tekrar eden kayıt ve yüksek kalorili aday birlikte verilir.
        candidates = [
            {"name": "Az uyumlu", "calories": 220, "protein": 10, "used": 2, "total": 4, "match_ratio": 0.5, "score": 80},
            {"name": "Çok uyumlu", "calories": 300, "protein": 12, "used": 3, "total": 3, "match_ratio": 1.0, "score": 60},
            {"name": "Yüksek kalorili", "calories": 520, "protein": 20, "used": 4, "total": 4, "match_ratio": 1.0, "score": 99},
            {"name": "Çok uyumlu", "calories": 300, "protein": 12, "used": 3, "total": 3, "match_ratio": 1.0, "score": 60},
        ]
        ranked = rank_recipe_candidates(candidates, "Kilo Verme")
        self.assertEqual([item["name"] for item in ranked], ["Çok uyumlu", "Az uyumlu"])

    def test_photo_scenario_does_not_start_with_low_match_api_recipe(self):
        """Fotoğraftan gelen malzemelerde yerel aramanın uygun sonucu öne aldığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Fotoğraf analizinden gelebilecek malzeme listesiyle yerel tarif araması yapılır.
        results = search_local_recipes(
            ["Labne", "Domates", "Peynir", "Salatalık"],
            "Kilo Verme",
        )
        self.assertTrue(results)
        self.assertEqual(results[0]["name"], "Akdeniz Salatası")
        self.assertTrue(all(float(item["calories"]) <= 450 for item in results))


class NutritionLabelTests(unittest.TestCase):
    def test_valid_label_json_is_normalized(self):
        """Geçerli etiket JSON'undaki sayısal değerlerin standart biçime dönüştüğünü sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Etiket analizi servisinin döndürebileceği JSON metni ayrıştırılır.
        result = parse_label_response(json.dumps({
            "has_nutrition_label": True,
            "product_name": "Protein Bar",
            "basis_type": "100 g",
            "calories_kcal": "345,5",
            "protein_g": 25,
            "match_score": 78,
            "positive_points": ["Protein içeriyor"],
            "attention_points": [],
            "unreadable_fields": [],
        }))
        self.assertEqual(result["product_name"], "Protein Bar")
        self.assertEqual(result["calories_kcal"], 345.5)
        self.assertEqual(result["protein_g"], 25.0)

    def test_missing_nutrients_remain_null(self):
        """Etikette bulunmayan besin alanlarının null kaldığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        result = parse_label_response('{"has_nutrition_label":true,"basis_type":"100 ml"}')
        self.assertIsNone(result["fiber_g"])
        self.assertIsNone(result["sodium_mg"])

    def test_broken_json_becomes_controlled_error(self):
        """Bozuk etiket JSON'unun kontrollü servis hatasına dönüştüğünü sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Ayrıştırma hatasının uygulamayı durdurmak yerine beklenen özel hatayı üretmesi beklenir.
        with self.assertRaises(NutritionLabelError):
            parse_label_response("geçersiz-json")

    def test_same_photo_and_goal_is_analyzed_once(self):
        """Aynı fotoğraf ve hedef için analiz sonucunun önbellekten tekrar kullanıldığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Analiz işlevinin kaç kez çağrıldığını saymak için bellek içi sayaç kullanılır.
        calls = {"count": 0}

        def analyzer():
            """Test için çağrı sayısını artırıp örnek analiz sonucu döndürür.

            Args:
                Bu iç fonksiyon dışarıdan değer almaz.

            Returns:
                dict: Analiz sonucunu temsil eden en küçük sözlük.
            """
            calls["count"] += 1
            return {"has_nutrition_label": True, "basis_type": "100 g"}

        cache = {}
        first = get_or_analyze_label(cache, "photo-hash", "Kas Yapma", analyzer)
        second = get_or_analyze_label(cache, "photo-hash", "Kas Yapma", analyzer)
        self.assertIs(first, second)
        self.assertEqual(calls["count"], 1)

    def test_100g_basis_is_not_changed_to_portion(self):
        """100 g ölçüsünün porsiyon ölçüsüne dönüştürülmediğini sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        result = parse_label_response(json.dumps({
            "has_nutrition_label": True,
            "basis_type": "100 g",
            "serving_size": None,
            "calories_kcal": 210,
            "detected_text_summary": "Porsiyon sütunu ayrıca görünüyordu.",
        }))
        self.assertEqual(result["basis_type"], "100 g")
        self.assertIsNone(result["serving_size"])
        self.assertEqual(result["calories_kcal"], 210.0)

    def test_food_photo_without_readable_label_is_rejected(self):
        """Besin tablosu olmayan görsel için tahmini analiz üretilmesini engeller."""
        raw_result = json.dumps({
            "has_nutrition_label": False,
            "basis_type": "bilinmiyor",
            "calories_kcal": 420,
            "protein_g": 18,
        })

        with self.assertRaisesRegex(
            NutritionLabelError,
            "Bu görselde okunabilir bir besin etiketi bulunamadı",
        ):
            parse_label_response(raw_result)


class MealAnalysisTests(unittest.TestCase):
    def test_gemini_http_errors_are_reported_separately(self):
        """Kota, anahtar ve model HTTP hatalarının farklı mesajlar ürettiğini sınar."""
        expected_messages = {
            429: GEMINI_QUOTA_MESSAGE,
            401: GEMINI_AUTH_MESSAGE,
            403: GEMINI_AUTH_MESSAGE,
            404: GEMINI_DEFAULT_ERROR_MESSAGE,
            503: GEMINI_OVERLOADED_MESSAGE,
        }
        for status_code, expected_text in expected_messages.items():
            with self.subTest(status_code=status_code):
                with self.assertRaisesRegex(MealAnalysisError, re.escape(expected_text)):
                    _raise_for_gemini_status(status_code)

    def test_gemini_error_messages_are_safe_and_fixed(self):
        """Ham Gemini hata ayrıntılarının yalnızca sabit Türkçe mesajlara eşlendiğini sınar."""
        self.assertEqual(gemini_error_message(503, response_text="Gemini is overloaded"), GEMINI_OVERLOADED_MESSAGE)
        self.assertEqual(gemini_error_message(500, response_text="UNAVAILABLE"), GEMINI_OVERLOADED_MESSAGE)
        self.assertEqual(gemini_error_message(429, response_text="RESOURCE_EXHAUSTED"), GEMINI_QUOTA_MESSAGE)
        self.assertEqual(gemini_error_message(error=requests.Timeout("secret-url")), GEMINI_TIMEOUT_MESSAGE)
        self.assertEqual(gemini_error_message(401, response_text="secret-api-key"), GEMINI_AUTH_MESSAGE)
        self.assertEqual(gemini_error_message(500, response_text="private raw error"), GEMINI_DEFAULT_ERROR_MESSAGE)

    def test_gemini_name_and_raw_error_are_not_sent_to_translator(self):
        """Gemini hata metninin çeviri servisine gönderilmediğini sınar."""
        translate_to_turkish.cache_clear()
        fake_translator = MagicMock()
        with patch("services.translator.GoogleTranslator", return_value=fake_translator):
            result = translate_to_turkish("Gemini is overloaded")
        translate_to_turkish.cache_clear()

        self.assertEqual(result, GEMINI_OVERLOADED_MESSAGE)
        fake_translator.translate.assert_not_called()

    def valid_payload(self):
        """Öğün analizi testlerinde kullanılan geçerli örnek yanıt sözlüğünü döndürür.

        Args:
            Bu yardımcı metot dışarıdan değer almaz.

        Returns:
            dict: Öğün analizi şemasına uygun test verisi.
        """
        return {
            "is_meal_image": True,
            "meal_name": "Örnek öğün",
            "items": [{
                "name": "Örnek ürün",
                "estimated_grams": 100,
                "calories_kcal": 120,
                "protein_g": 8,
                "carbohydrates_g": 15,
                "fat_g": 3,
                "fiber_g": 2,
                "confidence": "orta",
            }],
            "overall_confidence": "orta",
            "goal_comment": "Yaklaşık bir değerlendirmedir.",
            "uncertainties": ["Pişirme yağı görünmüyor."],
        }

    def test_valid_gemini_json_is_parsed(self):
        """Geçerli öğün JSON'unun ayrıştırılıp toplamlarının hesaplandığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Servis yanıtını temsil eden JSON metni öğün verisine dönüştürülür.
        result = parse_meal_response(json.dumps(self.valid_payload(), ensure_ascii=False))
        self.assertTrue(result["is_meal_image"])
        self.assertEqual(result["items"][0]["estimated_grams"], 100.0)
        self.assertEqual(calculate_meal_totals(result["items"])["protein_g"], 8.0)

    def test_markdown_json_block_is_parsed(self):
        """Markdown kod bloğuna sarılmış JSON yanıtının ayrıştırıldığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        raw = "```json\n" + json.dumps(self.valid_payload(), ensure_ascii=False) + "\n```"
        self.assertEqual(parse_meal_response(raw)["meal_name"], "Örnek öğün")

    def test_invalid_json_is_a_controlled_error(self):
        """Geçersiz öğün JSON'unun kontrollü servis hatasına dönüştüğünü sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        with self.assertRaises(MealAnalysisError):
            parse_meal_response("{bozuk-json")

    def test_missing_fields_are_safe(self):
        """Eksik alanlı öğün yanıtının güvenli varsayılan değerlerle işlendiğini sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        result = parse_meal_response('{"is_meal_image": true}')
        self.assertEqual(result["items"], [])
        self.assertEqual(result["overall_confidence"], "düşük")
        self.assertEqual(result["uncertainties"], [])

    def test_non_meal_image_is_preserved(self):
        """Yemek içermeyen görsel bilgisinin yanıt içinde korunduğunu sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        result = parse_meal_response('{"is_meal_image": false, "items": []}')
        self.assertFalse(result["is_meal_image"])

    def test_null_nutrients_remain_null(self):
        """Null gelen besin değerlerinin değişmeden korunduğunu sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Örnek yanıtın lif değeri bilinmiyor olarak ayarlanır.
        payload = self.valid_payload()
        payload["items"][0]["fiber_g"] = None
        result = parse_meal_response(json.dumps(payload, ensure_ascii=False))
        self.assertIsNone(result["items"][0]["fiber_g"])

    def test_gram_change_scales_nutrients_locally(self):
        """Gram miktarı değiştiğinde besin değerlerinin yerel olarak orantılandığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Başlangıç öğün verisi yeniden API çağrısı yapılmadan yerelde güncellenir.
        current = parse_meal_response(json.dumps(self.valid_payload(), ensure_ascii=False))["items"]
        updated, warnings = scale_meal_items(current, [{"Ürün adı": "Örnek ürün", "Tahmini gram": 200}])
        self.assertEqual(updated[0]["calories_kcal"], 240.0)
        self.assertEqual(updated[0]["protein_g"], 16.0)
        self.assertEqual(warnings, [])

    def test_zero_and_negative_grams_are_rejected(self):
        """Sıfır ve negatif gram girişlerinin servis hatası oluşturduğunu sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        current = parse_meal_response(json.dumps(self.valid_payload(), ensure_ascii=False))["items"]
        # Her geçersiz gram değeri bağımsız alt test olarak doğrulanır.
        for invalid_grams in (0, -25):
            with self.subTest(grams=invalid_grams):
                with self.assertRaises(MealAnalysisError):
                    scale_meal_items(current, [{"Ürün adı": "Örnek ürün", "Tahmini gram": invalid_grams}])

    def test_new_image_clears_old_active_result(self):
        """Yeni görsel yüklendiğinde önceki analiz sonucunun oturumdan silindiğini sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Streamlit session state benzeri sözlükte eski görsele ait veriler oluşturulur.
        state = {
            "meal_analysis_photo_hash": "eski-hash",
            "meal_analysis_active_result": {"data": "eski"},
            "meal_analysis_error": "eski hata",
        }
        self.assertTrue(reset_meal_state_for_new_image(state, "yeni-hash"))
        self.assertEqual(state["meal_analysis_photo_hash"], "yeni-hash")
        self.assertNotIn("meal_analysis_active_result", state)
        self.assertNotIn("meal_analysis_error", state)

    def test_same_image_does_not_call_analyzer_twice(self):
        """Aynı görsel ve hedef için analiz işlevinin ikinci kez çağrılmadığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Mock, gerçek Gemini API çağrısı olmadan analiz işlevinin çağrısını izler.
        analyzer = Mock(return_value={"is_meal_image": True, "items": []})
        cache = {}
        get_or_analyze_meal(cache, "aynı-hash", "Dengeli Beslenme", analyzer)
        get_or_analyze_meal(cache, "aynı-hash", "Dengeli Beslenme", analyzer)
        analyzer.assert_called_once()

    def test_same_daily_meal_is_not_added_twice(self):
        """Aynı analiz kimliğine sahip öğünün günlük listeye tekrar eklenmediğini sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Aynı kayıt iki kez eklenmek istenir; ikinci ekleme reddedilmelidir.
        daily_meals = []
        record = {"analysis_id": "tekil-analiz", "meal_name": "Örnek öğün"}
        self.assertTrue(add_daily_meal(daily_meals, record))
        self.assertFalse(add_daily_meal(daily_meals, record))
        self.assertEqual(len(daily_meals), 1)

    def test_runtime_schema_has_no_fixed_meal_result(self):
        """Çalışma zamanı şemasında sabit yemek adı veya varsayılan sonuç bulunmadığını sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Gemini yanıt şemasındaki ürün adı alanının sabit değer içermediği doğrulanır.
        item_schema = meal_response_schema()["properties"]["items"]["items"]["properties"]
        self.assertNotIn("default", item_schema["name"])
        self.assertNotIn("enum", item_schema["name"])


class DailyMealDateTests(unittest.TestCase):
    """Günüm ekranının yerel gün filtresini gerçek API kullanmadan doğrular."""

    def test_three_different_dates_are_grouped_by_selected_day(self):
        """Üç farklı tarihli öğünden yalnızca seçili tarihin kayıtlarının geldiğini sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # Farklı günlere ait örnek kayıtlar, uygulamanın kullandığı ISO tarih biçimiyle hazırlanır.
        meals = [
            {"analysis_id": "onceki", "datetime": "2026-08-21T14:30:00+03:00"},
            {"analysis_id": "secilen", "datetime": "2026-08-22T09:15:00+03:00"},
            {"analysis_id": "bugun", "datetime": "2026-08-23T19:45:00+03:00"},
        ]
        selected = meals_for_date(meals, date(2026, 8, 22))
        self.assertEqual([meal["analysis_id"] for meal in selected], ["secilen"])

    def test_utc_record_uses_local_calendar_day(self):
        """UTC ile saklanan kaydın kullanıcının yerel gününe dönüştürüldüğünü sınar.

        Args:
            Bu test metodu dışarıdan değer almaz.

        Returns:
            None: Başarısızlıkta unittest doğrulama hatası üretir.
        """
        # İstanbul saatinde 00.30 olan kayıt UTC'de bir önceki gün 21.30 olarak saklanabilir.
        record = {"datetime": "2026-08-22T21:30:00+00:00"}
        self.assertEqual(local_meal_date(record), date(2026, 8, 23))


class MongoDailyMealStoreTests(unittest.TestCase):
    """MongoDB öğün deposunun Atlas'a bağlanmadan temel sözleşmesini doğrular."""

    def setUp(self):
        """Her test için arayüzün kullandığı örnek kayıt sözlüğünü hazırlar."""
        self.record = {
            "analysis_id": "mongo-test-1",
            "meal_name": "Test öğünü",
            "datetime": "2026-08-23T14:30:00+03:00",
            "goal": "Dengeli Beslenme",
            "items": [],
            "total_calories_kcal": 320,
            "total_protein_g": 20,
            "total_carbohydrates_g": 35,
            "total_fat_g": 11,
            "total_fiber_g": 6,
            "image_hash": "hash-1",
        }

    def test_mongodb_document_uses_real_datetime(self):
        """MongoDB belgesinde tarihin metin yerine gerçek datetime olduğunu sınar."""
        document = _record_to_document(self.record)
        self.assertIsInstance(document["created_at"], datetime)
        restored = _document_to_record(document)
        self.assertEqual(restored["analysis_id"], self.record["analysis_id"])
        self.assertIn("T", restored["datetime"])

    def test_crud_functions_keep_boolean_and_list_contracts(self):
        """Ekleme, listeleme, güncelleme ve silmenin eski dönüş türlerini koruduğunu sınar."""
        collection = Mock()
        insert_result = Mock(upserted_id="new-document")
        update_result = Mock(matched_count=1)
        collection.update_one.side_effect = [insert_result, update_result]
        delete_result = Mock(deleted_count=1)
        collection.delete_one.return_value = delete_result
        cursor = Mock()
        cursor.sort.return_value = [_record_to_document(self.record)]
        collection.find.return_value = cursor

        with patch("services.daily_meal_store.initialize_daily_meal_store"), patch(
            "services.daily_meal_store._collection", return_value=collection
        ):
            self.assertTrue(save_daily_meal(self.record))
            self.assertEqual(load_daily_meals()[0]["analysis_id"], "mongo-test-1")
            self.assertTrue(update_daily_meal("mongo-test-1", {"meal_name": "Yeni ad"}))
            self.assertTrue(delete_daily_meal("mongo-test-1"))

    def test_sqlite_migration_is_idempotent(self):
        """Aynı SQLite aktarımının ikinci çağrıda tekrar kayıt üretmediğini sınar."""
        collection = MagicMock()
        migrations = MagicMock()
        collection.database.__getitem__.return_value = migrations
        migrations.find_one.side_effect = [None, {"_id": "sqlite_daily_meals_v1"}]
        collection.update_one.return_value = Mock(upserted_id="migrated-document")

        with patch("services.daily_meal_store._sqlite_records", return_value=[self.record]):
            self.assertEqual(_migrate_sqlite_records_once(collection), 1)
            self.assertEqual(_migrate_sqlite_records_once(collection), 0)
        self.assertEqual(collection.update_one.call_count, 1)
        self.assertEqual(migrations.update_one.call_count, 1)

    def test_legacy_claim_updates_only_documents_without_user_id(self):
        """Eski öğün geçişinin yalnızca user_id alanı bulunmayan belgeleri bağladığını sınar."""
        collection = MagicMock()
        migrations = MagicMock()
        collection.database.__getitem__.return_value = migrations
        migrations.find_one.side_effect = [None, {"claimed_by": "google-user-1"}]
        collection.update_many.return_value = Mock(modified_count=3)

        with patch("services.daily_meal_store.initialize_daily_meal_store"), patch(
            "services.daily_meal_store._collection", return_value=collection
        ):
            claimed = claim_legacy_daily_meals_for_user("google-user-1")

        self.assertEqual(claimed, 3)
        collection.update_many.assert_called_once()
        self.assertEqual(
            collection.update_many.call_args.args[0],
            {"user_id": {"$exists": False}},
        )
        self.assertEqual(
            collection.update_many.call_args.args[1]["$set"]["user_id"],
            "google-user-1",
        )

    def test_legacy_claim_cannot_be_taken_by_another_user(self):
        """Bir hesap için kilitlenen eski öğünlerin başka hesaba aktarılmadığını sınar."""
        collection = MagicMock()
        migrations = MagicMock()
        collection.database.__getitem__.return_value = migrations
        migrations.find_one.return_value = {"claimed_by": "google-user-1", "status": "completed"}

        with patch("services.daily_meal_store.initialize_daily_meal_store"), patch(
            "services.daily_meal_store._collection", return_value=collection
        ):
            self.assertEqual(claim_legacy_daily_meals_for_user("google-user-2"), 0)
            self.assertEqual(count_claimable_legacy_daily_meals("google-user-2"), 0)

        collection.update_many.assert_not_called()
        collection.count_documents.assert_not_called()

    def test_tls_error_is_reported_without_server_address(self):
        """TLS bağlantı hatasının gizli sunucu ayrıntısı olmadan açıklanmasını sınar."""
        error = ServerSelectionTimeoutError("SSL handshake failed: gizli-sunucu.example:27017")
        category = _mongo_failure_category(error)
        message = _public_mongo_error(category, "Genel hata")

        self.assertEqual(category, "tls_handshake")
        self.assertIn("güvenli bağlantı", message)
        self.assertNotIn("gizli-sunucu", message)

    def test_authentication_error_has_clear_public_message(self):
        """Kimlik doğrulama hatasının kullanıcıya anlaşılır biçimde dönmesini sınar."""
        error = OperationFailure("Authentication failed", code=18)
        category = _mongo_failure_category(error)
        message = _public_mongo_error(category, "Genel hata")

        self.assertEqual(category, "authentication")
        self.assertIn("kullanıcı adı veya parolası", message)


if __name__ == "__main__":
    # Dosya doğrudan çalıştırılırsa unittest test keşfini başlatır.
    unittest.main()
