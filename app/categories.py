"""Starter categories. They are copied into the database once and can then be edited in the app."""

DEFAULT_CATEGORIES: dict[str, str] = {
    "Meat & Seafood": "Fresh or packaged meat, poultry, fish, shellfish, deli meats, sausages",
    "Produce": "Fresh fruit, vegetables, herbs, salad kits",
    "Dairy & Eggs": "Milk, cheese, yogurt, butter, cream, eggs, plant milks",
    "Bakery & Bread": "Bread, rolls, tortillas, bagels, cakes and pastries from the bakery",
    "Pantry": "Rice, pasta, flour, canned goods, sauces, spices, oils, cereal, baking supplies",
    "Snacks & Sweets": "Chips, crackers, cookies, candy, chocolate, nuts, granola bars, ice cream treats",
    "Beverages": "Soda, juice, coffee, tea, water, sports and energy drinks (non-alcoholic)",
    "Frozen": "Frozen meals, frozen vegetables, frozen pizza, frozen desserts other than treats",
    "Prepared Food": "Ready-to-eat meals, rotisserie chicken, hot bar, sushi, sandwiches",
    "Alcohol": "Beer, wine, spirits, hard seltzer",
    "Household": "Cleaning supplies, paper towels, trash bags, foil, batteries, bags",
    "Personal Care": "Toiletries, cosmetics, medicine, vitamins",
    "Baby & Pet": "Diapers, baby food, formula, pet food and supplies",
    "Other": "Anything that does not fit the categories above (deposits, gift cards, fees)",
}

# Catch-all that items fall back to when their category is deleted. It cannot be renamed or deleted.
FALLBACK_CATEGORY = "Other"
