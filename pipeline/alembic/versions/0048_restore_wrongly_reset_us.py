"""Restore hq_country='US' on the 495 rows #259's first run reset by mistake

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-09 00:00:00.000000

#259 added a normalize-hq-state pass that resets an unevidenced ``hq_country =
'US'`` to NULL. Its predicate was too broad: it also matched rows with
NEITHER an hq_state NOR an hq_city. The tier-3 "state/city present → US" rule
it was repairing could never have produced those rows' "US" — it came from
another path, typically an explicit judge-eligibility verdict, which is not
stored in the enrich payload. The first prod run (pipeline run 37891595208,
2026-10-09 06:04 UTC, --limit 500) reset 500 rows; 495 had no state and no
city. The predicate is narrowed in the same PR. This migration puts those 495
back exactly as they were.

Guarded so it only restores rows still in the post-reset state (country NULL,
no state, no city, never checked by infer-hq-country). A row anything else
has touched since is left alone, so it is idempotent and safe to re-run. The
slug list comes from that run's log ("unevidenced US reset (slug=… state=None
city=None)"). Downgrade is a no-op: re-nulling would re-apply the bug.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESET_SLUGS: tuple[str, ...] = (
    "11x", "42", "7cups", "abby-care", "abinitio-bio", "adaptive", "adfin",
    "adialante", "advanced-metal-research", "aimon", "airgarage", "airops", "airtime",
    "airtop", "aiwyn", "akkari", "aktis-oncology", "alara", "albacore", "albert",
    "aleo", "aleph", "algox2", "alivecor", "alleviate-health", "allowance", "alluxio",
    "almanac-health", "alooma", "alpine-bio", "alt-x", "altur", "amber",
    "ambience-healthcare", "ambition", "amika", "amperity", "anoria", "anrok",
    "answerthis", "anto-biosciences", "anyroad", "appzen", "archal", "archive",
    "arctic-health", "arcwise", "area-1-security", "arzana", "aseon-labs", "asserts",
    "asseta", "astrus", "at-bay", "atlys", "atrisa-formerly-refortifai", "autositu",
    "auxos", "avarra", "axoni", "azra-games", "backflip-ai", "bankjoy", "base-power",
    "basis-theory", "bastille-networks", "bastion", "beacon-health", "beamery",
    "beehiiv", "beme", "benchling", "betterup", "bigeye", "bizzy-ai",
    "black-ore-technologies", "blockaid", "blue-origin", "braintrust", "brex",
    "brisk-teaching", "buildzoom", "bunkerhill-health", "buxfer", "cactus",
    "capitolis", "carbonated", "casetext", "castle", "catena-labs", "causaly",
    "cellino", "checkr", "chert", "chronicle-labs", "ciphercloud", "circle-medical",
    "circuithub", "clara", "clarity", "clerk", "clerky", "cloudinary", "code-four",
    "codecombat", "coder", "cofactor-genomics", "cofia", "coiled", "comma-ai",
    "common-room", "commonwealth-fusion-systems-cfs", "compyle", "confluence-labs",
    "confluera", "constellation-space", "convex", "convexia", "copilotiq",
    "counsel-health", "crosslayer-labs", "crusoe", "curai", "curri", "cylake",
    "daily-harvest", "daivin", "dashlane", "datasnipper", "datoric", "decagon",
    "desktop-metal", "deso", "dev-agents", "diligencesquared", "diode",
    "ditto-biosciences", "divvy-homes", "donotpay", "doppel", "doubleai", "doxel",
    "draftwise", "duranium", "dynamic-labs", "dyspatch", "ease-health", "easypost",
    "eclypsium", "egenesis", "eigenpal", "elegen", "engflow", "eon", "equal-iq",
    "estimote", "ethos", "etleap", "everest", "everyrealm", "exonic",
    "expected-parrot", "expel", "experiment", "faraway", "farther", "fashivly",
    "fieldguide", "fiftythree", "fin", "finaloop", "finix", "firefly-health", "fizz",
    "flash-hoops", "flip", "flock-homes", "flock-safety", "formation", "found",
    "foundation", "foundation-industries", "foxglove", "frame-security",
    "framewise-health", "fresha", "front", "function-health", "fundbox", "furtherai",
    "gc-therapeutics", "gem", "general-astronautics", "general-matter", "gigfinesse",
    "givecampus", "glimpse", "glossgenius", "goat-group", "gobble", "gocardless",
    "godhands", "goldbelly", "good-technology", "goop", "granola", "graphite", "gravy",
    "greentoe", "gremlin", "grubmarket", "guild", "gusto", "haladir", "harvey",
    "hasura", "headspace-health", "heroic-labs", "hessian", "hevn", "hive",
    "human-archive", "human-dx", "human-interest", "hypercubic", "illoca", "incention",
    "industrial-microbes", "infinitus", "inflammatix", "inviscid-ai", "janet-ai",
    "joopiter", "jump", "jumprope", "k-id", "kaedim", "keep", "kestrel-ai", "keycard",
    "keyframe-labs", "kikoff", "kimpton-ai", "klaimee", "komodo-health", "kwelitv",
    "lab0", "langfuse", "lark", "level-frames", "levels-health", "liberate-bio",
    "life360", "lightsource", "lightspark", "limitless-labs", "linzumi", "lithic",
    "lob", "logosguard", "loom", "loop", "lugg", "machine0", "macroscope",
    "magic-eden", "magic-leap", "mandolin", "manifold", "markit", "mayvenn", "mazama",
    "maze-therapeutics", "mealpal", "medikine", "meitre", "memverge",
    "midstream-health", "mimos", "mind-robotics", "minro", "mod-ai", "modernfi",
    "modular", "monaco", "monarcha", "moonwalk-biosciences", "moov", "multi",
    "multifactor", "muni", "mux", "mythical-games", "nationbuilder", "navan", "neeva",
    "nerviom", "newsblur", "nextdoor", "nine-fives", "nomagic", "nominal",
    "north-pole-security", "nova-credit", "numerion-labs", "nurx", "nx", "observo-ai",
    "octant-bio", "octapulse", "offerup", "onaroll", "onehouse", "onesignal",
    "onshape", "onxmaps", "opengov", "opensea", "orange-slice", "orb", "orthogonal",
    "oshi-health", "outsmart-college", "overdrive-health", "overture-life",
    "palus-finance", "panorama-education", "parachute", "paramark", "pave", "payall",
    "payna", "paytient", "pearl-bio", "pebble", "perfectly", "petcube", "philon",
    "physical-intelligence", "physical-turing", "picnicai", "picogrid", "pilotgpt",
    "pipedrive", "pivotal", "plate-iq", "point", "pomelo-care", "posterous", "prisms",
    "procindex", "proclaim", "proof-of-play", "prototyping-io", "prox", "pryzm",
    "pug-ai", "pushbullet", "pyn", "qomplement", "quartzy", "quotain", "qventus",
    "radar", "raindrop", "ramp", "raspire", "redpanda", "reduct", "reducto",
    "regrello", "remedio", "remedy", "replicate", "replit", "resolve", "rev",
    "rezo-therapeutics", "rhumbix", "rightway-healthcare", "rillet", "rive",
    "robodock", "roebling", "rohirrim", "rollup-ai", "rovi-health", "rudus",
    "rune-technologies", "ruvo", "rylo", "saffron", "sandbox-vr", "scoop",
    "scribe-therapeutics", "scylladb", "sentrial", "sf-tensor", "sfox",
    "sharp-performance", "shipper", "shippo", "shopmonkey", "shortwave", "sigmanticai",
    "signalfx", "sleeper", "smack", "smartcar", "snackpass", "snapmagic", "snorkel",
    "socratix-ai", "sola", "solv", "somnee", "sourcegraph", "soylent", "spire",
    "spotpay", "stacker", "stealth-worker", "stilta", "stockline", "strand-ai",
    "strella", "strikingly", "styleseat", "styleup", "supermove", "superset",
    "swaybrand", "swell", "symbolica", "synctera", "tectoai", "telogis", "tennr",
    "terranox-ai", "testerarmy", "thesis", "thinking-machines-lab", "thrive-agritech",
    "thunkable", "thyme-care", "tilt", "timbucktoo", "toma", "tovala", "traceroot-ai",
    "trucksmarter", "true-link", "truemed", "truevault", "truffle-security",
    "turquoise-health", "tydo", "ultima-genomics", "unconventional-ai", "unifold",
    "uno-wallet", "unsiloed-ai", "upstart", "upstash", "usergems", "vannevar-labs",
    "vectra-ai", "verkada", "vero-biosciences", "vestwell", "vibeflow", "virtru",
    "vision-lab", "vitally", "voquill", "voxel51", "watershed", "wavedash",
    "wealthmore", "weee", "wepay", "whatnot", "wingspan", "wiz", "wonderschool",
    "workboard", "workos", "world-labs", "xai", "yaysay", "zatanna", "zentail",
    "zerosettle", "zipline", "zus-health",
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE companies SET hq_country = 'US' "
            "WHERE slug = ANY(:slugs) "
            "AND hq_country IS NULL "
            "AND hq_state IS NULL "
            "AND hq_city IS NULL "
            "AND hq_country_checked_at IS NULL"
        ),
        {"slugs": list(_RESET_SLUGS)},
    )


def downgrade() -> None:
    # Intentionally a no-op: re-nulling these rows would re-apply the bug.
    pass
