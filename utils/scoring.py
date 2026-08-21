def calculate_recipe_score(
    calories: float,
    protein: float,
    carbohydrates: float,
    fat: float,
    used_ingredients: int,
    total_ingredients: int,
    goal: str,
) -> int:
    """Score a recipe with transparent, goal-specific rules."""
    protein_score = min(100, protein * 3)
    calorie_score = max(0, 100 - (calories / 6))
    carbohydrate_score = max(0, 100 - (carbohydrates * 1.5))
    fat_score = max(0, 100 - (fat * 2))
    ingredient_score = (
        (used_ingredients / total_ingredients) * 100 if total_ingredients else 0
    )

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
    """Hedefle açıkça çelişen tariflerin öneri listesine girmesini engelle."""
    calories = float(calories or 0)
    protein = float(protein or 0)
    if calories <= 0:
        return True
    if goal == "Kilo Verme":
        return calories <= 450
    if goal == "Kas Yapma":
        return protein >= 15 and calories <= 900
    return calories <= 900
