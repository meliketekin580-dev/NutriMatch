import unittest
import json
from unittest.mock import Mock

from utils.scoring import calculate_recipe_score, is_recipe_suitable_for_goal
from services.recipe_service import rank_recipe_candidates, search_local_recipes
from services.nutrition_label_service import (
    NutritionLabelError,
    get_or_analyze_label,
    parse_label_response,
)
from services.meal_analysis_service import (
    MealAnalysisError,
    _response_schema as meal_response_schema,
    add_daily_meal,
    calculate_meal_totals,
    get_or_analyze_meal,
    parse_meal_response,
    reset_meal_state_for_new_image,
    scale_meal_items,
)


class RecipeScoringTests(unittest.TestCase):
    def test_score_is_an_integer(self):
        score = calculate_recipe_score(420, 28, 35, 14, 3, 5, "Dengeli Beslenme")
        self.assertIsInstance(score, int)

    def test_matching_ingredients_improve_score(self):
        partial = calculate_recipe_score(300, 20, 25, 10, 2, 5, "Dengeli Beslenme")
        complete = calculate_recipe_score(300, 20, 25, 10, 5, 5, "Dengeli Beslenme")
        self.assertGreater(complete, partial)

    def test_muscle_goal_rewards_more_protein(self):
        lower = calculate_recipe_score(500, 10, 40, 15, 4, 5, "Kas Yapma")
        higher = calculate_recipe_score(500, 30, 40, 15, 4, 5, "Kas Yapma")
        self.assertGreater(higher, lower)

    def test_weight_loss_rejects_extreme_calorie_recipe(self):
        self.assertFalse(is_recipe_suitable_for_goal(1368, 69, "Kilo Verme"))
        self.assertTrue(is_recipe_suitable_for_goal(320, 8, "Kilo Verme"))

    def test_all_sources_use_same_ranking_and_goal_filter(self):
        candidates = [
            {"name": "Az uyumlu", "calories": 220, "protein": 10, "used": 2, "total": 4, "match_ratio": 0.5, "score": 80},
            {"name": "Çok uyumlu", "calories": 300, "protein": 12, "used": 3, "total": 3, "match_ratio": 1.0, "score": 60},
            {"name": "Yüksek kalorili", "calories": 520, "protein": 20, "used": 4, "total": 4, "match_ratio": 1.0, "score": 99},
            {"name": "Çok uyumlu", "calories": 300, "protein": 12, "used": 3, "total": 3, "match_ratio": 1.0, "score": 60},
        ]
        ranked = rank_recipe_candidates(candidates, "Kilo Verme")
        self.assertEqual([item["name"] for item in ranked], ["Çok uyumlu", "Az uyumlu"])

    def test_photo_scenario_does_not_start_with_low_match_api_recipe(self):
        results = search_local_recipes(
            ["Labne", "Domates", "Peynir", "Salatalık"],
            "Kilo Verme",
        )
        self.assertTrue(results)
        self.assertEqual(results[0]["name"], "Akdeniz Salatası")
        self.assertTrue(all(float(item["calories"]) <= 450 for item in results))


class NutritionLabelTests(unittest.TestCase):
    def test_valid_label_json_is_normalized(self):
        result = parse_label_response(json.dumps({
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
        result = parse_label_response('{"basis_type":"100 ml"}')
        self.assertIsNone(result["fiber_g"])
        self.assertIsNone(result["sodium_mg"])

    def test_broken_json_becomes_controlled_error(self):
        with self.assertRaises(NutritionLabelError):
            parse_label_response("geçersiz-json")

    def test_same_photo_and_goal_is_analyzed_once(self):
        calls = {"count": 0}

        def analyzer():
            calls["count"] += 1
            return {"basis_type": "100 g"}

        cache = {}
        first = get_or_analyze_label(cache, "photo-hash", "Kas Yapma", analyzer)
        second = get_or_analyze_label(cache, "photo-hash", "Kas Yapma", analyzer)
        self.assertIs(first, second)
        self.assertEqual(calls["count"], 1)

    def test_100g_basis_is_not_changed_to_portion(self):
        result = parse_label_response(json.dumps({
            "basis_type": "100 g",
            "serving_size": None,
            "calories_kcal": 210,
            "detected_text_summary": "Porsiyon sütunu ayrıca görünüyordu.",
        }))
        self.assertEqual(result["basis_type"], "100 g")
        self.assertIsNone(result["serving_size"])
        self.assertEqual(result["calories_kcal"], 210.0)


class MealAnalysisTests(unittest.TestCase):
    def valid_payload(self):
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
        result = parse_meal_response(json.dumps(self.valid_payload(), ensure_ascii=False))
        self.assertTrue(result["is_meal_image"])
        self.assertEqual(result["items"][0]["estimated_grams"], 100.0)
        self.assertEqual(calculate_meal_totals(result["items"])["protein_g"], 8.0)

    def test_markdown_json_block_is_parsed(self):
        raw = "```json\n" + json.dumps(self.valid_payload(), ensure_ascii=False) + "\n```"
        self.assertEqual(parse_meal_response(raw)["meal_name"], "Örnek öğün")

    def test_invalid_json_is_a_controlled_error(self):
        with self.assertRaises(MealAnalysisError):
            parse_meal_response("{bozuk-json")

    def test_missing_fields_are_safe(self):
        result = parse_meal_response('{"is_meal_image": true}')
        self.assertEqual(result["items"], [])
        self.assertEqual(result["overall_confidence"], "düşük")
        self.assertEqual(result["uncertainties"], [])

    def test_non_meal_image_is_preserved(self):
        result = parse_meal_response('{"is_meal_image": false, "items": []}')
        self.assertFalse(result["is_meal_image"])

    def test_null_nutrients_remain_null(self):
        payload = self.valid_payload()
        payload["items"][0]["fiber_g"] = None
        result = parse_meal_response(json.dumps(payload, ensure_ascii=False))
        self.assertIsNone(result["items"][0]["fiber_g"])

    def test_gram_change_scales_nutrients_locally(self):
        current = parse_meal_response(json.dumps(self.valid_payload(), ensure_ascii=False))["items"]
        updated, warnings = scale_meal_items(current, [{"Ürün adı": "Örnek ürün", "Tahmini gram": 200}])
        self.assertEqual(updated[0]["calories_kcal"], 240.0)
        self.assertEqual(updated[0]["protein_g"], 16.0)
        self.assertEqual(warnings, [])

    def test_zero_and_negative_grams_are_rejected(self):
        current = parse_meal_response(json.dumps(self.valid_payload(), ensure_ascii=False))["items"]
        for invalid_grams in (0, -25):
            with self.subTest(grams=invalid_grams):
                with self.assertRaises(MealAnalysisError):
                    scale_meal_items(current, [{"Ürün adı": "Örnek ürün", "Tahmini gram": invalid_grams}])

    def test_new_image_clears_old_active_result(self):
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
        analyzer = Mock(return_value={"is_meal_image": True, "items": []})
        cache = {}
        get_or_analyze_meal(cache, "aynı-hash", "Dengeli Beslenme", analyzer)
        get_or_analyze_meal(cache, "aynı-hash", "Dengeli Beslenme", analyzer)
        analyzer.assert_called_once()

    def test_same_daily_meal_is_not_added_twice(self):
        daily_meals = []
        record = {"analysis_id": "tekil-analiz", "meal_name": "Örnek öğün"}
        self.assertTrue(add_daily_meal(daily_meals, record))
        self.assertFalse(add_daily_meal(daily_meals, record))
        self.assertEqual(len(daily_meals), 1)

    def test_runtime_schema_has_no_fixed_meal_result(self):
        item_schema = meal_response_schema()["properties"]["items"]["items"]["properties"]
        self.assertNotIn("default", item_schema["name"])
        self.assertNotIn("enum", item_schema["name"])


if __name__ == "__main__":
    unittest.main()
