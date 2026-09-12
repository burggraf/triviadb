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
    "book-author": ("Q7725634", "P50", "Books & Language", "Books & Authors", "author of the literary work"),
    "play-author": ("Q25379", "P50", "Books & Language", "Plays & Playwrights", "author of the play"),
    "painting-artist": ("Q3305213", "P170", "Arts & Culture", "Paintings & Artists", "creator of the painting"),
    "sculpture-artist": ("Q860861", "P170", "Arts & Culture", "Sculpture", "creator of the sculpture"),
    "building-architect": ("Q41176", "P84", "History", "Landmarks & Architecture", "architect of the building"),
    "bridge-designer": ("Q12280", "P84", "Geography", "Bridges & Landmarks", "architect of the bridge"),
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
