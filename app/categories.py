"""The fixed set of spending categories every receipt line is sorted into."""

CATEGORIES: dict[str, str] = {
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

CATEGORY_NAMES: list[str] = list(CATEGORIES)
