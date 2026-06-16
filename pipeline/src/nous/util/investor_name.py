"""Investor name canonicalization.

Without normalization, "Sequoia", "Sequoia Capital", and "SEQUOIA CAPITAL"
would each be a separate investor row. We strip common firm-suffix words and
lowercase to derive a stable lookup key.

The display name (preserved separately on `investors.name`) keeps the
original casing as the LLM extracted it.
"""

from __future__ import annotations

import re

# Trailing words to strip when computing the normalized key. Matched
# case-insensitively at the end of the name (with optional preceding
# whitespace). Repeat-stripped to handle e.g. "Acme Partners LP".
_SUFFIX_PATTERN = re.compile(
    r"\s+\b(capital|ventures?|partners?|management|group|fund|lp|llc)\b\.?$",
    re.IGNORECASE,
)

# Conservative alias map: only unambiguous, true-duplicate firm names.
#
# Rationale for conservatism: we only map names that refer to the *exact same
# legal entity* and where there is no reasonable ambiguity. "a16z" and
# "Andreessen Horowitz" are the same firm; "GV" and "Google Ventures" are
# the same firm. By contrast, named sub-funds of the same family (e.g.
# "Valor Equity Partners" vs "Valor Atreides AI Fund") are DIFFERENT entities
# and are explicitly left un-aliased. When in doubt, leave it out — merging
# distinct entities is far worse than leaving mild duplicates in the DB.
#
# Keys and values are the POST-suffix-stripped, lowercased canonical forms.
# All entries are bidirectional (both sides map to the canonical "winner").
_ALIAS_PAIRS: list[tuple[str, str]] = [
    # a16z ↔ Andreessen Horowitz (same firm, commonly known by both names)
    ("a16z", "andreessen horowitz"),
    # GV (formerly Google Ventures) ↔ Google Ventures
    ("gv", "google"),
    # New Enterprise Associates ↔ NEA
    ("nea", "new enterprise associates"),
    # General Atlantic — GA is a common abbreviation
    ("ga", "general atlantic"),
    # Institutional Venture Partners ↔ IVP
    ("ivp", "institutional venture"),
    # Battery Ventures ↔ Battery
    ("battery", "battery"),
]

# Build a flat alias map: each key maps to the canonical form (the
# alphabetically-first member of the pair, for determinism).
_ALIAS_MAP: dict[str, str] = {}
for _a, _b in _ALIAS_PAIRS:
    _canonical = min(_a, _b)
    _ALIAS_MAP[_a] = _canonical
    _ALIAS_MAP[_b] = _canonical


def canonicalize_investor_name(name: str) -> str:
    """Return the normalized lookup key for an investor name.

    Lowercase, suffix-stripped, whitespace-collapsed. Alias-mapped so
    common variant names (e.g. "a16z" and "Andreessen Horowitz") resolve to
    the same canonical key. Empty input returns the empty string — callers
    should check before inserting.

    Note: only the *bare* "a16z" aliases to Andreessen Horowitz. "a16z Crypto"
    is a genuinely distinct fund — its suffix is not in ``_SUFFIX_PATTERN`` so
    it survives as ``"a16z crypto"`` and never collides with the parent firm.

    Examples:
        "Sequoia Capital"             -> "sequoia"
        "Lightspeed Venture Partners" -> "lightspeed"
        "Founders Fund"               -> "founders"
        "a16z"                        -> "a16z"
        "Andreessen Horowitz"         -> "a16z"
        "a16z Crypto"                 -> "a16z crypto"  (distinct fund — not merged)
        "GV"                          -> "google"
        "Google Ventures"             -> "google"
        "YC"                          -> "yc"
    """
    cleaned = name.strip()
    prev: str | None = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _SUFFIX_PATTERN.sub("", cleaned).strip()
    cleaned = cleaned.lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Apply alias map — resolve to the canonical name for this equivalence class.
    return _ALIAS_MAP.get(cleaned, cleaned)


# ---------------------------------------------------------------------------
# Junk / placeholder investor names
# ---------------------------------------------------------------------------
#
# Funding articles routinely describe rounds with non-name placeholders —
# "raised from a group of investors", "backed by undisclosed investors",
# "existing investors participated". The LLM faithfully extracts these as
# investor "names", which then become bogus investor rows (e.g. the live
# "a group of investors" row backing 2 companies). They carry no identity, so
# they must never become rows and any existing ones are deleted by
# dedup-investors.
#
# Detection is intentionally CONSERVATIVE: matched against the canonicalized
# (suffix-stripped, lowercased) key so casing/"Capital"/"Partners" suffixes
# don't matter, and only EXACT-equality against a hand-curated set plus a few
# tightly-anchored patterns. We never substring-match a junk word inside a real
# firm name — "Founders Fund" contains "fund" but is a real firm; "New
# Enterprise Associates" is real despite "enterprise". The rule of thumb mirrors
# the alias map: a false positive (deleting a real investor) is far worse than a
# false negative (leaving one junk row), so when in doubt we leave it in.

# Exact canonical keys that are never real investors. These are the
# canonicalize_investor_name() forms — e.g. "angel investors" has no strippable
# suffix so it stays "angel investors"; "Strategic Investors" -> "strategic
# investors". Kept as canonical forms so the check is a single set lookup.
_JUNK_CANONICALS: frozenset[str] = frozenset(
    {
        # Generic "investors" placeholders
        "investors",
        "investor",
        "angel investors",
        "angel investor",
        "strategic investors",
        "strategic investor",
        "existing investors",
        "existing investor",
        "new investors",
        "new investor",
        "individual investors",
        "institutional investors",
        "private investors",
        "various investors",
        "other investors",
        "additional investors",
        "undisclosed investors",
        "unnamed investors",
        # "a group of investors" and near-variants — these survive
        # canonicalization because "group" is only stripped as a *trailing*
        # suffix word, and here it is mid-phrase ("group of investors"). Listed
        # explicitly anyway (the pattern below also catches them).
        "a group of investors",
        "group of investors",
        "a consortium of investors",
        "consortium of investors",
        "a syndicate of investors",
        "syndicate of investors",
        # Bare placeholders
        "undisclosed",
        "undisclosed investor",
        "n/a",
        "na",
        "none",
        "unknown",
        "various",
        "anonymous",
        "confidential",
        "tbd",
        "tba",
        "others",
        "et al",
        "et al.",
        # Round descriptors mis-extracted as investors
        "self funded",
        "self-funded",
        "bootstrapped",
        "crowdfunding",
        "the public",
    }
)

# Anchored patterns for junk that varies too much to enumerate. Each is matched
# against the canonical key with ``fullmatch`` so it only fires on the WHOLE
# name — never a substring of a real firm. Deliberately narrow.
_JUNK_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "a group of investors", "the group of angel investors", "a number of
    # strategic investors", etc. — any "<article> ... investors" placeholder.
    re.compile(
        r"(a|an|the|several|multiple|numerous|various|other|"
        r"existing|new|undisclosed|unnamed|anonymous|strategic|"
        r"group of|number of|consortium of|syndicate of|"
        r"\s)+"
        r"(angel\s+|institutional\s+|individual\s+|strategic\s+|private\s+)?"
        r"investors?",
    ),
    # Pure punctuation / empty-ish (e.g. "-", "--", "?", "n.a.").
    re.compile(r"[\W_]+"),
    re.compile(r"n\.?\s*/?\s*a\.?"),
)


def is_junk_investor_name(name: str) -> bool:
    """Return True when *name* is a non-investor placeholder, not a real firm.

    Detects the article-phrasing artifacts that the funding-extraction LLM
    turns into bogus investor rows — "a group of investors", "undisclosed",
    "existing investors", "angel investors" (as a generic, not a named angel),
    "n/a", "various", and obvious variants.

    Conservative by construction: the input is canonicalized first (so casing
    and firm suffixes are irrelevant), then checked by EXACT set membership and
    a few whole-string-anchored patterns. A real firm name is never matched —
    "Founders Fund", "New Enterprise Associates", "Group 11", "Angel Investors
    Network LLC"-style proper firms do not collapse to a bare placeholder key,
    so they pass through. When unsure, we return False (keep the row).

    Empty / whitespace-only input is junk (True) — a blank name can never be a
    real investor and the upsert path already rejects empty canonical keys.
    """
    canonical = canonicalize_investor_name(name)
    if not canonical:
        return True
    if canonical in _JUNK_CANONICALS:
        return True
    return any(p.fullmatch(canonical) for p in _JUNK_PATTERNS)


# ---------------------------------------------------------------------------
# Individual (angel) vs institutional firm classification
# ---------------------------------------------------------------------------
#
# Investor rows extracted from funding news mix institutional firms ("Sequoia
# Capital") with named individuals ("Jeff Bezos", "Elon Musk") who invest as
# angels. The directory currently lists individuals alongside firms with no
# distinction. We classify a name as an individual (angel) when it looks like a
# human's name AND carries none of the tokens that mark a firm/fund.
#
# Heuristic (deliberately conservative — mislabeling a FIRM as a person is the
# costly error, so the bar for "individual" is high):
#   * 2 or 3 whitespace-separated tokens (most personal names; "Marc Andreessen",
#     "John A. Doe"). One-token names are too ambiguous (could be "Sequoia") and
#     4+ tokens are almost always firms ("New Enterprise Associates Partners").
#   * Every token is alphabetic (allowing a single trailing-dot initial like
#     "A."). Digits, "&", "/", etc. signal a firm ("Group 11", "B&Y").
#   * NONE of the firm-marker tokens appear (capital, ventures, partners, fund,
#     group, holdings, llc, inc, labs, …) — case-insensitive, whole-token.
#   * **The first token is a recognized given name** (``_GIVEN_NAMES``). This is
#     the key guard against surname-pair FIRM names: "Andreessen Horowitz",
#     "Kleiner Perkins", "Draper Fisher", "Bessemer", "Tiger Global" all pass the
#     token-count + firm-token checks (two alphabetic words, no firm marker) yet
#     are firms, not people. Requiring a known first name rejects them while
#     still catching "Jeff Bezos", "Elon Musk", "Reid Hoffman", etc. The known-
#     firm map (only ~13 scraped firms) is far too small to be this backstop on
#     its own — most firms in the DB arrive via funding news, not the registry.
#   * Not a known scraped-firm canonical and not junk.
# Anything failing these stays unclassified (the caller leaves type as-is). The
# cost of a missed angel (left 'unknown') is a cosmetic directory gap; the cost
# of a misclassified firm (a famous VC tagged 'angel') is a visible data error.
# The given-name gate trades recall for that precision deliberately.

# Whole-token markers that mean "this is a firm/fund, not a person." Matched
# case-insensitively against each lowercased token of the ORIGINAL display name
# (not the canonical key, which strips several of these as suffixes).
_FIRM_TOKENS: frozenset[str] = frozenset(
    {
        "capital",
        "ventures",
        "venture",
        "partners",
        "partner",
        "fund",
        "funds",
        "group",
        "holdings",
        "holding",
        "llc",
        "llp",
        "lp",
        "inc",
        "inc.",
        "incorporated",
        "corp",
        "corp.",
        "corporation",
        "co",
        "co.",
        "company",
        "labs",
        "lab",
        "management",
        "advisors",
        "advisers",
        "associates",
        "equity",
        "investments",
        "investment",
        "ventures,",
        "partners,",
        "trust",
        "foundation",
        "bank",
        "fintech",
        "technologies",
        "technology",
        "systems",
        "global",
        "international",
        "enterprises",
        "enterprise",
        "growth",
        "asset",
        "assets",
        "securities",
        "financial",
        "industries",
        "collective",
        "syndicate",
        "consortium",
        "accelerator",
        "incubator",
        "studio",
        "studios",
        "network",
        "fellowship",
        "office",
        "family",
    }
)

# A token shaped like a personal-name word: an initial like "A." / "A", or a
# capitalized/lowercased alphabetic word, optionally hyphenated ("Jean-Luc") or
# with an apostrophe ("O'Brien"). Non-ASCII letters are allowed.
_NAME_TOKEN = re.compile(r"^[^\W\d_]+(?:[-'’][^\W\d_]+)*\.?$", re.UNICODE)

# Recognized given (first) names. The first token of a candidate individual
# name must be in this set. This is the precision lever that keeps surname-pair
# FIRM names ("Andreessen Horowitz", "Kleiner Perkins", "Tiger Global") out of
# the angel bucket. The list leans toward common US/Western and well-known
# tech-founder/angel first names; it is intentionally broad enough to catch the
# bulk of real angels without ever matching a firm's leading word. Adding names
# here only ever INCREASES angel recall — a firm name will still be rejected by
# the firm-token / token-shape checks even if its first word were added.
_GIVEN_NAMES: frozenset[str] = frozenset(
    {
        # Male
        "aaron", "abdul", "abe", "abraham", "adam", "adrian", "ahmed", "aidan",
        "akira", "al", "alan", "albert", "alec", "alejandro", "alex",
        "alexander", "alexandre", "alexei", "alfred", "ali", "allen", "alon",
        "alvin", "amir", "andre", "andreas", "andrei", "andrew", "andy",
        "angus", "anil", "anthony", "antoine", "antonio", "arash", "ari",
        "arjun", "armand", "arnold", "arthur", "arun", "asa", "ash", "ashwin",
        "austin", "avi", "barry", "ben", "benedict", "benjamin", "bernard",
        "bert", "bill", "billy", "blake", "bo", "bob", "bobby", "boris", "brad",
        "bradley", "brandon", "brendan", "brent", "bret", "brett", "brian",
        "bruce", "bruno", "bryan", "byron", "caleb", "calvin", "cameron",
        "carl", "carlos", "casey", "cedric", "chad", "charles", "charlie",
        "chase", "chen", "chester", "chip", "chris", "christian", "christopher",
        "chuck", "clark", "claude", "clayton", "clement", "cliff", "clifford",
        "clint", "clinton", "clive", "cody", "cole", "colin", "conor", "connor",
        "cooper", "corey", "cory", "craig", "curtis", "cyrus", "dale", "damian",
        "damien", "damon", "dan", "dana", "daniel", "danny", "darren", "dave",
        "david", "dean", "deepak", "demetri", "dennis", "derek", "desmond",
        "dev", "devin", "dexter", "diego", "dimitri", "dinesh", "dion", "dirk",
        "dmitri", "dmitry", "dominic", "don", "donald", "doug", "douglas",
        "drew", "duncan", "dustin", "dylan", "earl", "ed", "eddie", "eddy",
        "eden", "edgar", "edmund", "eduardo", "edward", "edwin", "eitan", "eli",
        "elias", "elijah", "elliot", "elliott", "elon", "emanuel", "emil",
        "emmanuel", "enzo", "eric", "erik", "ernest", "ernie", "ethan",
        "eugene", "evan", "ezra", "fabio", "felix", "fernando", "finn",
        "florian", "floyd", "francis", "francisco", "frank", "franklin", "fred",
        "freddie", "frederick", "gabe", "gabriel", "gareth", "gary", "gavin",
        "gene", "geoff", "geoffrey", "george", "gerald", "gerard", "gideon",
        "gil", "giles", "glenn", "gordon", "graham", "grant", "greg", "gregg",
        "gregory", "guido", "gustav", "guy", "hank", "hans", "harold", "harry",
        "harvey", "hassan", "hector", "henri", "henry", "herbert", "herman",
        "hideo", "hiroshi", "homer", "horace", "howard", "hugh", "hugo", "hunter",
        "ian", "ibrahim", "ignacio", "igor", "ilya", "ira", "irving", "isaac",
        "ismail", "ivan", "jack", "jackson", "jacob", "jacques", "jaime",
        "jake", "jamal", "james", "jamie", "jan", "jared", "jarrod", "jason",
        "javier", "jay", "jean", "jeff", "jefferson", "jeffrey", "jens", "jerry",
        "jesse", "jim", "jimmy", "joachim", "joaquin", "joe", "joel", "joey",
        "johan", "johann", "john", "johnny", "jon", "jonah", "jonas",
        "jonathan", "jordan", "jorge", "jose", "josef", "joseph", "josh",
        "joshua", "juan", "jude", "julian", "julien", "julius", "justin",
        "kai", "kareem", "karl", "karthik", "kaspar", "keith", "kelvin", "ken",
        "kendall", "kenneth", "kenny", "kent", "kevin", "khalid", "kieran",
        "kim", "kirk", "klaus", "kristian", "kunal", "kurt", "kyle", "lance",
        "lars", "laurent", "lawrence", "lee", "leland", "len", "leo", "leon",
        "leonard", "leonardo", "leroy", "leslie", "lester", "lev", "lewis",
        "liam", "lionel", "logan", "lorenzo", "lou", "louis", "luc", "luca",
        "lucas", "luigi", "luis", "luke", "lyle", "mahesh", "malcolm", "manuel",
        "marc", "marcel", "marco", "marcus", "mario", "mark", "marshall",
        "martin", "marty", "marvin", "mason", "mateo", "mathias", "matt",
        "matteo", "matthew", "matthias", "maurice", "max", "maxim", "maximilian",
        "maxwell", "mehul", "mel", "melvin", "micah", "michael", "michel",
        "miguel", "mike", "mikhail", "miles", "milo", "milton", "mitch",
        "mitchell", "mo", "mohamed", "mohammed", "morgan", "morris", "moshe",
        "murray", "nabil", "naoki", "naseem", "nash", "nat", "nate", "nathan",
        "nathaniel", "naveen", "neal", "ned", "neil", "nelson", "nicholas",
        "nick", "nico", "nicolas", "nigel", "nikhil", "niklas", "nikolai",
        "nils", "noah", "noam", "noel", "norman", "oliver", "olivier", "omar",
        "oscar", "otis", "otto", "owen", "pablo", "parker", "pascal", "pat",
        "patrick", "paul", "pavel", "pedro", "percy", "pete", "peter", "phil",
        "philip", "philippe", "phillip", "pierre", "piotr", "preston", "quentin",
        "quincy", "rafael", "raj", "rajesh", "ralph", "ram", "ramon", "randall",
        "randy", "raphael", "rashid", "ravi", "ray", "raymond", "reed", "reggie",
        "reginald", "reid", "rene", "reuben", "rex", "rhys", "ricardo",
        "richard", "rick", "ricky", "rishi", "rob", "robert", "roberto", "robin",
        "rod", "roderick", "rodney", "rodrigo", "roger", "roland", "rolf", "ron",
        "ronald", "ronnie", "rory", "ross", "roy", "ruben", "rudy", "rufus",
        "rupert", "russ", "russell", "ryan", "sachin", "salman", "sam",
        "sameer", "samir", "samuel", "sanjay", "santiago", "saul", "scott",
        "sean", "sebastian", "serge", "sergei", "sergey", "seth", "shane",
        "shaun", "shawn", "sheldon", "sherman", "shinya", "sid", "sidney",
        "silas", "simon", "sol", "solomon", "spencer", "stan", "stanley",
        "stefan", "stephan", "stephen", "stephane", "steve", "steven", "stewart",
        "stuart", "sumit", "sunil", "sven", "syed", "tad", "tariq", "ted",
        "terence", "terrance", "terrell", "terrence", "terry", "thaddeus",
        "theo", "theodore", "thomas", "tim", "timothy", "tobias", "toby", "todd",
        "tom", "tomas", "tommy", "tony", "travis", "trent", "trevor", "tristan",
        "troy", "tucker", "tyler", "tyrone", "ulrich", "umar", "uri", "valentin",
        "vance", "varun", "vasili", "vector", "vernon", "victor", "vijay",
        "vikram", "vince", "vincent", "vinod", "viraj", "virgil", "vishal",
        "vladimir", "wade", "wallace", "walter", "warren", "wayne", "wei",
        "wendell", "werner", "wes", "wesley", "wilbur", "wilfred", "will",
        "willard", "william", "willie", "willis", "wilson", "winston", "wolfgang",
        "wyatt", "xavier", "yann", "yannick", "yaron", "yi", "yoav", "yossi",
        "yuri", "yusuf", "yves", "zach", "zachary", "zack", "zane", "zeke",
        "zhang", "zhao", "ziv",
        # Female
        "abby", "abigail", "ada", "adriana", "agatha", "agnes", "aimee",
        "aisha", "alana", "alexa", "alexandra", "alexis", "alice", "alicia",
        "alison", "allison", "alyssa", "amanda", "amber", "amelia", "amy", "ana",
        "anastasia", "andrea", "angela", "angelica", "angelina", "anita", "ann",
        "anna", "annabel", "anne", "annette", "annie", "antonia", "april",
        "arielle", "ashley", "astrid", "aubrey", "audrey", "ava", "barbara",
        "beatrice", "becky", "belinda", "bella", "bernadette", "beth", "bethany",
        "betsy", "betty", "beverly", "bianca", "bonnie", "brenda", "bridget",
        "brittany", "brooke", "camille", "candace", "cara", "carla", "carmen",
        "carol", "carole", "caroline", "carolyn", "carrie", "cassandra",
        "catherine", "cathy", "cecilia", "celeste", "celia", "chantal",
        "charlene", "charlotte", "chelsea", "cheryl", "chloe", "christina",
        "christine", "cindy", "claire", "clara", "clarissa", "claudia",
        "colleen", "connie", "constance", "cora", "courtney", "crystal",
        "cynthia", "daisy", "daniela", "danielle", "daphne", "darlene",
        "dawn", "deanna", "debbie", "deborah", "debra", "delia", "denise",
        "diana", "diane", "dina", "dolores", "dominique", "donna", "dora",
        "doreen", "doris", "dorothy", "edith", "eileen", "elaine", "eleanor",
        "elena", "eliana", "elisa", "elise", "elizabeth", "ella", "ellen",
        "eloise", "elsa", "emily", "emma", "erica", "erika", "erin", "esther",
        "ethel", "eva", "evangeline", "eve", "evelyn", "faith", "farah", "fay",
        "felicia", "fiona", "flora", "florence", "frances", "francesca",
        "freya", "gabriela", "gabrielle", "gail", "gemma", "genevieve",
        "georgia", "georgina", "geraldine", "gertrude", "gillian", "gina",
        "ginny", "giselle", "gladys", "glenda", "gloria", "grace", "greta",
        "gretchen", "gwen", "gwendolyn", "hailey", "haley", "halle", "hana",
        "hannah", "harriet", "hazel", "heather", "heidi", "helen", "helena",
        "henrietta", "hilary", "hillary", "holly", "hope", "ida", "imogen",
        "ingrid", "irene", "iris", "isabel", "isabella", "isabelle", "ivy",
        "jacqueline", "jade", "jane", "janet", "janice",
        "jasmine", "jeanette", "jeanne", "jenna", "jennifer", "jenny",
        "jessica", "jill", "joan", "joanna", "joanne", "jocelyn", "jodie",
        "joelle", "johanna", "josephine", "joy", "joyce", "juana", "judith",
        "judy", "julia", "juliana", "julie", "juliet", "juliette", "june",
        "kaitlyn", "kara", "karen", "kari", "karin", "karina", "kate",
        "katelyn", "katharine", "katherine", "kathleen", "kathryn", "kathy",
        "katie", "katrina", "kay", "kayla", "keiko", "kelly", "kelsey",
        "kendra", "kerry", "kimberly", "kira", "krista", "kristen",
        "kristin", "kristina", "kristine", "krystal", "lana", "lara", "laura",
        "lauren", "laurie", "leah", "leila", "lena", "leona", "lila",
        "lillian", "lily", "linda", "lindsay", "lindsey", "lisa", "liv", "liz",
        "lizzie", "lois", "lola", "loretta", "lori", "lorraine", "louisa",
        "louise", "lucia", "lucille", "lucy", "luisa", "lydia", "lynn", "mabel",
        "mackenzie", "madeline", "madison", "maggie", "maia", "mandy", "mara",
        "marcia", "margaret", "margarita", "margot", "maria", "mariam", "marian",
        "marianne", "marie", "marilyn", "marina", "marion", "marisa", "marjorie",
        "marlene", "marsha", "martha", "mary", "maureen", "maya", "meaghan",
        "meg", "megan", "meghan", "melanie", "melinda", "melissa", "mercedes",
        "meredith", "mia", "michaela", "michele", "michelle", "mila",
        "mildred", "millie", "mimi", "mindy", "miranda", "miriam", "misha",
        "mollie", "molly", "mona", "monica", "monique", "muriel",
        "myra", "myrna", "nadia", "nadine", "nancy", "naomi", "natalia",
        "natalie", "natasha", "neha", "nell", "nelly", "nicole", "nina", "nora",
        "norah", "norma", "octavia", "olga", "olive", "olivia", "opal", "ophelia",
        "page", "paige", "pam", "pamela", "patricia", "patsy", "patti", "paula",
        "pauline", "pearl", "peggy", "penelope", "penny", "phoebe", "phyllis",
        "polly", "portia", "preeti", "priscilla", "priya", "rachel", "ramona",
        "raquel", "rebecca", "regina", "renata", "renee", "rhoda", "rhonda",
        "rita", "roberta", "rochelle", "rosa", "rosalind", "rose",
        "rosemary", "roxanne", "ruby", "ruth", "sabrina", "sadie", "sally",
        "samantha", "sandra", "sandy", "sara", "sarah", "sasha", "savannah",
        "selena", "selina", "serena", "shannon", "sharon", "sheila", "shelby",
        "shelley", "sherry", "shirley", "sienna", "silvia", "simone", "sofia",
        "sonia", "sonya", "sophia", "sophie", "stacey", "stacy", "stella",
        "stephanie", "sue", "susan", "susanna", "susanne", "suzanne", "sybil",
        "sylvia", "tabitha", "talia", "tamara", "tammy", "tania", "tanya",
        "tara", "tatiana", "teresa", "teri", "terri", "tess", "tessa", "thea",
        "thelma", "theresa", "tiffany", "tina", "toni", "tonya", "tracey",
        "tracy", "trinity", "trudy", "ursula", "valentina", "valerie", "vanessa",
        "vera", "verna", "veronica", "vicki", "vicky", "victoria", "viola",
        "violet", "virginia", "vivian", "vivienne", "wanda", "wendy", "whitney",
        "willa", "wilma", "winifred", "yael", "yasmin", "yoko", "yolanda",
        "yvette", "yvonne", "zara", "zoe", "zoey",
    }
)


def is_individual_investor_name(name: str, *, known_firm: bool = False) -> bool:
    """Return True when *name* looks like an individual (angel), not a firm.

    Used to classify investor rows as ``type='angel'``. See the module comment
    above for the full heuristic. ``known_firm`` lets the caller short-circuit
    to False when the name is already known to be a scraped institutional firm
    (its canonical key is in the firm registry) — a belt-and-suspenders guard
    on top of the firm-token check, so a registry firm whose name happens to
    have no firm-marker token (e.g. a one-word brand) is still never mislabeled.

    Returns True only when ALL hold:
      * 2-3 whitespace tokens,
      * no firm-marker token (``_FIRM_TOKENS``),
      * every token is a name-shaped word (letters / single initial),
      * the FIRST token is a recognized given name (``_GIVEN_NAMES``).

    Returns False otherwise — junk names, known firms, firm-token names,
    surname-pair firms ("Andreessen Horowitz", "Kleiner Perkins"), names with
    digits/symbols, and anything whose first token isn't a known given name.
    Conservative by design: when in doubt, NOT an individual.
    """
    if known_firm:
        return False
    if is_junk_investor_name(name):
        return False

    display = re.sub(r"\s+", " ", name.strip())
    if not display:
        return False

    tokens = display.split(" ")
    if not (2 <= len(tokens) <= 3):
        return False

    lowered = [t.lower() for t in tokens]
    if any(t in _FIRM_TOKENS for t in lowered):
        return False

    # Every token must look like a human-name word (letters / initials only).
    if not all(_NAME_TOKEN.match(t) for t in tokens):
        return False

    # The first token must be a recognized given name. This is the precision
    # guard that rejects surname-pair firm names ("Andreessen Horowitz",
    # "Kleiner Perkins", "Tiger Global") which otherwise satisfy every check
    # above. A trailing dot on an initial-style first token ("A. Smith") is not
    # a given name, so such names stay unclassified — acceptable, since a bare
    # initial is too ambiguous to confidently call an individual.
    return lowered[0] in _GIVEN_NAMES
