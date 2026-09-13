"""Fixed major categories and conservative, immutable fact recipes."""

CATEGORIES = (
    "Movies", "Television", "Music", "Books & Language", "History", "Geography",
    "Science & Technology", "Nature", "Sports & Games", "Arts & Culture", "Food & Drink", "Space",
)

# key: (Wikidata class, property, major category, reusable subcategory, relationship)
# Recipes keep discovery queries bounded; dump imports use the same catalog.
RECIPES = {
    "film-director": ("Q11424", "P57", "Movies", "Movie Directors", "director of the film"),
    "film-composer": ("Q11424", "P86", "Movies", "Film Scores", "composer of the film score"),
    "film-screenwriter": ("Q11424", "P58", "Movies", "Screenwriters", "screenwriter of the film"),
    "tv-creator": ("Q5398426", "P170", "Television", "TV Creators", "creator of the television series"),
    "album-artist": ("Q482994", "P175", "Music", "Albums & Artists", "performer of the album"),
    "song-composer": ("Q7366", "P86", "Music", "Songwriters", "composer of the song"),
    "song-performer": ("Q7366", "P175", "Music", "Songs & Performers", "performer of the song"),
    "book-author": ("Q7725634", "P50", "Books & Language", "Books & Authors", "author of the literary work"),
    "play-author": ("Q25379", "P50", "Books & Language", "Plays & Playwrights", "author of the play"),
    "painting-artist": ("Q3305213", "P170", "Arts & Culture", "Paintings & Artists", "creator of the painting"),
    "sculpture-artist": ("Q860861", "P170", "Arts & Culture", "Sculpture", "creator of the sculpture"),
    "building-architect": ("Q41176", "P84", "History", "Landmarks & Architecture", "architect of the building"),
    "bridge-designer": ("Q12280", "P84", "Geography", "Bridges & Landmarks", "architect of the bridge"),
    "country-capital": ("Q6256", "P36", "Geography", "Countries & Capitals", "capital of the country"),
    "country-currency": ("Q6256", "P38", "Geography", "Countries & Currencies", "currency used by the country"),
    "country-continent": ("Q6256", "P30", "Geography", "Countries & Continents", "continent containing the country"),
    "sports-club-sport": ("Q847017", "P641", "Sports & Games", "Sports Clubs", "sport played by the club"),
    "athlete-sport": ("Q2066131", "P641", "Sports & Games", "Athletes & Sports", "sport associated with the athlete"),
    "game-developer": ("Q7889", "P178", "Sports & Games", "Video Games", "developer of the video game"),
    "boardgame-designer": ("Q131436", "P287", "Sports & Games", "Board Games", "designer of the board game"),
    "element-number": ("Q11344", "P1086", "Science & Technology", "The Periodic Table", "atomic number of the element"),
    "element-symbol": ("Q11344", "P246", "Science & Technology", "Chemical Symbols", "chemical symbol of the element"),
    "scientist-field": ("Q901", "P101", "Science & Technology", "Scientists & Fields", "field of work of the scientist"),
    "mineral-system": ("Q7946", "P556", "Nature", "Minerals & Crystals", "crystal system of the mineral"),
    "dish-origin": ("Q746549", "P495", "Food & Drink", "Food Origins", "country of origin of the dish (reject disputed origins)"),
    "invention-inventor": ("Q12579633", "P61", "History", "Inventions & Inventors", "inventor of the invention"),
    "historical-figure-country": ("Q5774265", "P27", "History", "Historical Figures", "country of citizenship of the historical figure"),
    "battle-location": ("Q178561", "P276", "History", "Battles & History", "location of the battle"),
    "taxon-status": ("Q16521", "P141", "Nature", "Animals & Conservation", "IUCN conservation status of the species"),
    "moon-parent": ("Q2537", "P397", "Space", "Moons & Planets", "planet or body orbited by the natural satellite"),
    "spacecraft-maker": ("Q40218", "P176", "Space", "Spacecraft & Missions", "manufacturer of the spacecraft"),
}

# These pools need a bounded Wikidata subclass/occupation path during discovery. Explicit IDs
# remain supported too; extraction still requires one unqualified answer claim.
SUBCLASS_RECIPES = frozenset({
    "sports-club-sport", "invention-inventor", "historical-figure-country", "battle-location",
    "taxon-status", "moon-parent", "athlete-sport", "scientist-field",
})
RECIPE_CLASS_PROPERTIES = {"athlete-sport": "P106", "scientist-field": "P106"}
DISCOVERY_CLASS_PATHS = {
    **{recipe: "wdt:P31/wdt:P279*" for recipe in SUBCLASS_RECIPES - set(RECIPE_CLASS_PROPERTIES)},
    **{recipe: "wdt:P106/wdt:P279*" for recipe in RECIPE_CLASS_PROPERTIES},
}

RELATIONSHIPS = {key: row[4] for key, row in RECIPES.items()}
RELATIONSHIPS["city-latitude"] = "city farthest north among exactly the four supplied cities (maximum latitude)"

# These relationships are valid source facts but poor default pub-trivia material:
# they reward specialist recall or generate the same narrow clue repeatedly. Keep them
# importable and available behind an explicit opt-in instead of pretending popularity is
# familiarity.
SPECIALIST_POOLS = frozenset({
    "bridge-designer", "building-architect", "city-latitude", "element-number",
    "element-symbol", "film-composer", "film-screenwriter", "game-developer",
    "met-works-on-paper", "spacecraft-maker",
})

# Sitelinks are only a rough prominence filter, never a quality guarantee. This gate
# keeps the default queue from spending its first calls on the long tail.
DEFAULT_MIN_POPULARITY = {
    "album-artist": 30,
    "book-author": 35,
    "dish-origin": 35,
    "film-director": 65,
    "painting-artist": 65,
    "sculpture-artist": 65,
    "song-composer": 30,
    "tv-creator": 35,
    "country-capital": 100,
    "country-currency": 100,
    "country-continent": 100,
    "sports-club-sport": 100,
    "athlete-sport": 80,
    "invention-inventor": 100,
    "historical-figure-country": 100,
    "battle-location": 100,
    "taxon-status": 100,
    "moon-parent": 70,
    "scientist-field": 100,
    "song-performer": 30,
}


def default_fact_eligible(fact, *, has_media=False, question_type=None, include_specialist=False):
    if include_specialist:
        return True
    if fact["pool"] in SPECIALIST_POOLS:
        return False
    # The Met's generic highlighted-art records are not a familiarity signal. Keep
    # them for attachment-led photo questions, where the image is the actual clue.
    if fact.get("source") == "met" and not has_media:
        return False
    if question_type and question_type != "text" and not has_media:
        return False
    if has_media and fact.get("source") == "met":
        return True
    minimum = DEFAULT_MIN_POPULARITY.get(fact["pool"])
    return minimum is None or fact["popularity"] >= minimum
