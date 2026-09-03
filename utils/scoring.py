"""Tariflerin malzeme ve besin hedefi açısından iç sıralamasını hesaplar.

Bu puan yalnızca arama sonuçlarını daha uygun tariflerden başlayarak sıralamak
için kullanılır. Kullanıcı arayüzünde tıbbi bir değerlendirme veya kesin sağlık
puanı olarak gösterilmez.
"""


def calculate_recipe_score(
    calories: float,
    protein: float,
    carbohydrates: float,
    fat: float,
    used_ingredients: int,
    total_ingredients: int,
    goal: str,
) -> int:
    """Tarifi besin değerleri, malzeme uyumu ve hedefe göre puanlar.

    Args:
        calories: Tarifin kalori değeri.
        protein: Tarifin protein miktarı.
        carbohydrates: Tarifin karbonhidrat miktarı.
        fat: Tarifin yağ miktarı.
        used_ingredients: Kullanıcının elinde bulunan malzeme sayısı.
        total_ingredients: Tarifin toplam malzeme sayısı.
        goal: Seçili beslenme hedefi.

    Returns:
        int: Ağırlıklı ölçütlerden hesaplanan yuvarlanmış puan.
    """
    # Her besin ölçütü önce 0-100 aralığında bir alt puana dönüştürülür.
    protein_score = min(100, protein * 3)
    calorie_score = max(0, 100 - (calories / 6))
    carbohydrate_score = max(0, 100 - (carbohydrates * 1.5))
    fat_score = max(0, 100 - (fat * 2))
    ingredient_score = (
        (used_ingredients / total_ingredients) * 100 if total_ingredients else 0
    )

    # Hedefe göre alt puanların ağırlıkları değiştirilir.
    if goal == "Kilo Verme":
        weights = (0.25, 0.35, 0.10, 0.10, 0.20)
    elif goal == "Kas Yapma":
        weights = (0.35, 0.10, 0.25, 0.10, 0.20)
    else:
        weights = (0.20, 0.20, 0.20, 0.20, 0.20)

    values = (
        protein_score,
        calorie_score,
        carbohydrate_score,
        fat_score,
        ingredient_score,
    )
    return round(sum(value * weight for value, weight in zip(values, weights)))


def is_recipe_suitable_for_goal(calories: float, protein: float, goal: str) -> bool:
    """Tarifin seçili hedefle açıkça çelişip çelişmediğini kontrol eder.

    Args:
        calories: Tarifin kalori değeri.
        protein: Tarifin protein miktarı.
        goal: Seçili beslenme hedefi.

    Returns:
        bool: Tarif hedefe uygunsa True, değilse False.
    """
    # Boş veya sıfır kalori verisi, uygunluk filtresinden doğrudan geçer.
    calories = float(calories or 0)
    protein = float(protein or 0)
    if calories <= 0:
        return True
    if goal == "Kilo Verme":
        return calories <= 450
    if goal == "Kas Yapma":
        return protein >= 15 and calories <= 900
    return calories <= 900
