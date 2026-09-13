"""Fixed major categories and conservative, immutable fact recipes."""

CATEGORIES = (
    "Movies", "Television", "Music", "Books & Language", "History", "Geography",
    "Science & Technology", "Nature", "Sports & Games", "Arts & Culture", "Food & Drink", "Space",
)

# key: (Wikidata class, property, major category, reusable subcategory, relationship)
# Exact classes keep discovery queries bounded; dump imports use the same catalog.
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
    "game-developer": ("Q7889", "P178", "Sports & Games", "Video Games", "developer of the video game"),
    "boardgame-designer": ("Q131436", "P287", "Sports & Games", "Board Games", "designer of the board game"),
    "element-number": ("Q11344", "P1086", "Science & Technology", "The Periodic Table", "atomic number of the element"),
    "element-symbol": ("Q11344", "P246", "Science & Technology", "Chemical Symbols", "chemical symbol of the element"),
    "mineral-system": ("Q7946", "P556", "Nature", "Minerals & Crystals", "crystal system of the mineral"),
    "dish-origin": ("Q746549", "P495", "Food & Drink", "Food Origins", "country of origin of the dish (reject disputed origins)"),
    "spacecraft-maker": ("Q40218", "P176", "Space", "Spacecraft & Missions", "manufacturer of the spacecraft"),
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
