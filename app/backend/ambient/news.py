"""World news headlines for the sign: RSS fetch, a strict hard-news filter, clustering, ranking.

stdlib only (urllib, xml.etree). Headlines only, never body text: descriptions are read for scoring
and clustering but never shown.

Filter in two steps:
  1. Hard excludes: sections (URL paths, feed categories) and title patterns for sport, culture,
     lifestyle, video/photo pages, opinion, quizzes, live-blog filler, and fluff words.
  2. A hard-news score from weighted terms (geopolitics, conflict, diplomacy, elections, disasters
     with casualties, economy at state level, leaders, hotspots, countries). A headline must reach
     MIN_SCORE and carry at least one core signal (not just a country name).
Then the same story across outlets is clustered (IDF-weighted token overlap) and ranked by
outlets x recency (12 h decay) x score.
"""
from __future__ import annotations

import email.utils
import gzip
import html
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("ledsign.ambient.news")

USER_AGENT = "LEDSignStudio/1.0"
FETCH_TIMEOUT = 15
REFRESH_SECONDS = 20 * 60
MAX_AGE_HOURS = 36
DECAY_HOURS = 12.0
MIN_SCORE = 6
TOP_N = 10
MAX_FEED_BYTES = 4 * 1024 * 1024

# Checked in October 2026. AP has no public feed any more (feeds.apnews.com does not
# resolve, apnews.com/index.rss is 403) and Reuters has none, so NYT World and Al Jazeera stand
# in. CBS and Sky work but lean to crime stories; they are off by default.
SOURCES: dict[str, dict] = {
    "bbc": {"tag": "BBC", "name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
            "default": True, "color": (255, 70, 70)},
    "npr": {"tag": "NPR", "name": "NPR World", "url": "https://feeds.npr.org/1004/rss.xml",
            "default": True, "color": (90, 150, 255)},
    "guardian": {"tag": "GUARDIAN", "name": "The Guardian World", "url": "https://www.theguardian.com/world/rss",
                 "default": True, "color": (80, 140, 255)},
    "dw": {"tag": "DW", "name": "DW World", "url": "https://rss.dw.com/rdf/rss-en-world",
           "default": True, "color": (60, 180, 255)},
    "f24": {"tag": "F24", "name": "France 24", "url": "https://www.france24.com/en/rss",
            "default": True, "color": (80, 200, 255)},
    "un": {"tag": "UN", "name": "UN News", "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
           "default": True, "color": (90, 170, 255)},
    "nyt": {"tag": "NYT", "name": "New York Times World", "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "default": True, "color": (220, 220, 220)},
    "aj": {"tag": "AJ", "name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml",
           "default": True, "color": (255, 170, 40)},
    "cbs": {"tag": "CBS", "name": "CBS News World", "url": "https://www.cbsnews.com/latest/rss/world",
            "default": False, "color": (120, 160, 255)},
    "sky": {"tag": "SKY", "name": "Sky News World", "url": "https://feeds.skynews.com/feeds/rss/world.xml",
            "default": False, "color": (255, 80, 80)},
}

# When two outlets carry the same story, the representative headline comes from the first of these
# that has it (plain wire-style headlines first).
PREFERRED = ["bbc", "npr", "aj", "f24", "dw", "un", "nyt", "guardian", "cbs", "sky"]


# ---------------------------------------------------------------- text cleanup

_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "″": '"', "«": '"', "»": '"',
    "–": "-", "—": "-", "―": "-", "−": "-", "‐": "-", "‑": "-",
    "…": "...", " ": " ", " ": " ", " ": " ", " ": " ", "·": "-",
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o",
    "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "þ": "th",
    "ð": "d", "ı": "i", "£": "GBP ", "€": "EUR ", "¥": "JPY ", "%": "%",
    "°": " deg",
}
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    """Plain ASCII for the pixel font: no HTML, accents transliterated, straight quotes, no emoji."""
    if not s:
        return ""
    s = html.unescape(_TAG_RE.sub(" ", html.unescape(s)))
    s = "".join(_CHAR_MAP.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in s)
    return _WS_RE.sub(" ", s).strip()


# ---------------------------------------------------------------- fetch and parse


@dataclass
class Item:
    source: str
    title: str
    link: str
    desc: str
    published: float | None   # epoch seconds
    categories: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el: ET.Element, *names: str) -> str:
    for c in el:
        if _local(c.tag) in names:
            if _local(c.tag) == "link" and not (c.text or "").strip():
                href = c.get("href")
                if href:
                    return href
            return "".join(c.itertext())
    return ""


def _parse_date(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_feed(source: str, data: bytes) -> list[Item]:
    """RSS 2.0, RSS 1.0 (RDF) or Atom."""
    root = ET.fromstring(data)
    out: list[Item] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = clean_text(_child_text(el, "title"))
        if not title:
            continue
        link = _child_text(el, "link", "guid").strip()
        desc = clean_text(_child_text(el, "description", "summary", "content", "encoded"))[:600]
        date = _parse_date(_child_text(el, "pubdate", "date", "published", "updated"))
        cats = [clean_text("".join(c.itertext()) or c.get("term") or "") for c in el
                if _local(c.tag) in ("category", "subject")]
        out.append(Item(source, title, link, desc, date, [c for c in cats if c]))
    return out


def _decode_body(raw: bytes, encoding: str | None) -> bytes:
    # UN News sends gzip whatever Accept-Encoding says.
    if raw[:2] == b"\x1f\x8b" or (encoding or "").lower() == "gzip":
        return gzip.decompress(raw)
    if (encoding or "").lower() == "deflate":
        return zlib.decompress(raw)
    return raw


def fetch_feed(source: str, url: str, timeout: float = FETCH_TIMEOUT) -> list[Item]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_FEED_BYTES + 1)
        enc = r.headers.get("Content-Encoding")
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("feed too large")
    return parse_feed(source, _decode_body(raw, enc))


# ---------------------------------------------------------------- the hard-news filter

def _rx(words: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


# Pages that are never hard news, by URL path.
EXCLUDE_URL = re.compile(
    r"/(?:sport|sports|football|soccer|cricket|tennis|golf|rugby|formula1|f1|motorsport|cycling|"
    r"culture|arts?|artanddesign|film|films|movies|music|books|stage|tv-and-radio|television|games|"
    r"lifeandstyle|lifestyle|life|style|fashion|food|recipes?|travel|wellness|health/wellbeing|"
    r"commentisfree|opinion|opinions|comment|editorials?|letters|obituaries|obituary|"
    r"av|reel|in-pictures|picture|pictures|gallery|galleries|photos?|programmes|sounds|"
    r"podcasts?|newsletters?|crosswords?|quiz|quizzes|puzzles?|horoscopes?|media|"
    r"science|technology|tech|money|business/personal-finance|education|society|"
    r"wellness|weather|entertainment|celebrity|royals?|shopping|deals|reviews?)/",
    re.I)

# Feed categories (Guardian, NYT and others tag items) that mark soft sections.
EXCLUDE_CATEGORIES = {
    "sport", "sports", "football", "soccer", "cricket", "tennis", "golf", "rugby union", "rugby league",
    "formula one", "motor sport", "cycling", "athletics", "olympic games", "boxing", "basketball",
    "culture", "film", "movies", "music", "books", "art and design", "television & radio", "stage",
    "games", "life and style", "lifestyle", "fashion", "food", "travel", "opinion", "obituaries",
    "celebrity", "television", "theater", "dance", "pop and rock", "wellness", "health and wellbeing",
    "royal family", "animals", "wildlife", "space", "astronomy", "science", "technology",
    "crosswords", "games and puzzles", "real estate", "style", "weddings", "recipes",
}

# Titles that are never hard news.
EXCLUDE_TITLE = re.compile(
    r"^(?:watch|video|listen|podcast|in pictures|pictures|photos?|gallery|quiz|opinion|comment|"
    r"editorial|letters?|review|recipe|the week in|week in pictures|today in history|"
    r"morning briefing|evening briefing|first edition|the briefing|newsletter|meet|world news in brief|"
    r"news in brief|what to know|what we know|how|why|and the)\b"
    r"|\b(?:in pictures|as it happened|live updates|latest updates|key takeaways|what we know so far|"
    r"evening news|morning news|world news tonight|news quiz|crossword|horoscope|"
    r"best photos|photos of the (?:day|week)|pictures of the (?:day|week)|"
    r"you need to know|explained in \d+|in charts|in maps)\b"
    r"|^\d{1,2}/\d{1,2}:"            # "10/9: CBS Evening News"
    r"|^[A-Z][a-z]+ \d{1,2},? \d{4}$",  # bare dates
    re.I)

# Fluff topics. Any hit drops the headline (checked on the title, and on the description
# when the title has no core hard-news term).
FLUFF = _rx([
    # sport
    r"football", r"soccer", r"cricket", r"tennis", r"golf(?:er)?s?", r"rugby", r"nba", r"nfl", r"nhl", r"mlb",
    r"f1", r"formula (?:one|1)", r"grand prix", r"olympics?", r"paralympics?", r"world cup", r"premier league",
    r"champions league", r"la liga", r"serie a", r"bundesliga", r"t20", r"odi", r"test match", r"wimbledon",
    r"marathon", r"tournament", r"striker", r"midfielder", r"goalkeeper", r"quarter-?final", r"semi-?final",
    r"hat-trick", r"innings", r"wickets?", r"batsman", r"batter", r"medal(?:s|list)?", r"athletes?",
    r"boxing", r"boxer", r"ufc", r"wrestl\w*", r"jockey", r"derby", r"coach(?:es)?", r"fans", r"club",
    r"stadium", r"league", r"cup final", r"hundred", r"century", r"knockout",
    # culture and celebrity
    r"celebrit\w*", r"actor", r"actress", r"film", r"films", r"movie", r"movies", r"box office", r"oscars?",
    r"grammys?", r"emmys?", r"baftas?", r"cannes", r"album", r"singer", r"rapper", r"pop star", r"band",
    r"concert", r"tour dates", r"festival", r"eurovision", r"k-pop", r"netflix", r"hollywood", r"bollywood",
    r"tv series", r"sitcom", r"novel", r"novelist", r"author", r"poet", r"museum", r"exhibition", r"artist",
    r"painting", r"sculpture", r"opera", r"ballet", r"theatre", r"theater", r"comedian", r"influencers?",
    r"tiktok", r"youtuber", r"instagram", r"viral", r"meme", r"selfie", r"royal baby", r"prince harry",
    r"meghan", r"kardashian", r"taylor swift", r"beyonce", r"fashion", r"model", r"catwalk", r"designer",
    r"beauty", r"make-?up", r"wedding", r"dating", r"romance", r"astronaut", r"spacewalk", r"rocket launch",
    r"nasa", r"spacex", r"asteroid", r"comet", r"telescope", r"dinosaurs?", r"fossils?", r"archaeolog\w*",
    r"ancient", r"pyramid", r"shipwreck",
    # lifestyle and travel
    r"recipes?", r"cooking", r"chef", r"restaurant", r"food", r"wine", r"beer", r"coffee", r"diet", r"recipe",
    r"travel", r"holiday", r"vacation", r"tourists?", r"tourism", r"hotel", r"cruise", r"beach", r"resort",
    r"skincare", r"fitness", r"workout", r"wellness", r"sleep", r"happiness", r"parenting", r"gardening",
    r"puppy", r"kitten", r"pets?", r"dogs?", r"cats?", r"zoo", r"panda", r"pandas", r"whales?", r"dolphins?",
    r"bears?", r"elephants?", r"penguins?", r"birds?", r"animals?", r"wildlife", r"species", r"crocodile",
    r"shark", r"snake", r"monkey", r"lion", r"tiger", r"horse",
    # quirky / human interest / filler
    r"quiz", r"crossword", r"puzzle", r"horoscope", r"bizarre", r"weird", r"quirky", r"strange",
    r"world record", r"guinness", r"lottery", r"jackpot", r"heartwarming", r"adorable", r"oldest",
    r"birthday", r"centenarian", r"reunited", r"long-lost", r"surfers?", r"hikers?", r"climbers?",
    r"treasure", r"in pictures", r"photos", r"watch", r"podcast", r"obituary", r"memoir",
    r"first words", r"tribute", r"anniversary of",
])

# Crime and court stories without a state dimension are not geopolitics.
CRIME = _rx([r"murder\w*", r"guilty", r"sentenced", r"jailed", r"convicted", r"stabb\w*", r"robbery",
             r"burglary", r"kidnapp\w*", r"rape", r"trial", r"charged", r"manslaughter", r"homicide",
             r"serial killer", r"police say", r"court hears", r"inquest", r"fraud", r"smuggl\w*",
             r"drug lord", r"cartel", r"gang", r"suspect"])
# Features and explainers dressed as news: drop.
SOFT_DROP = _rx([r"through the lens", r"history of", r"life in", r"a day in", r"letter from", r"diary",
                 r"meet the", r"inside the", r"inside \w+'s", r"the story of", r"in his own words",
                 r"in her own words", r"what it's like", r"a year of", r"portrait of"])
# Opinion and soft framing: lower, not drop.
SOFT = _rx([r"analysis", r"explainer", r"how to", r"why you", r"what it means", r"what is", r"who is",
            r"inside the", r"the story of", r"life in", r"meet the", r"a day", r"letter from", r"diary",
            r"interview", r"profile", r"essay", r"review", r"through the lens", r"history of",
            r"reveals?", r"looks? back", r"voices"])

CORE_TERMS = [
    # conflict and security
    (5, [r"war", r"wars", r"ceasefire", r"truce", r"armistice", r"invasion", r"invade[sd]?", r"offensive",
         r"airstrikes?", r"air strikes?", r"missiles?", r"drone strikes?", r"drone attacks?", r"shelling",
         r"bombardment", r"troops", r"soldiers", r"military", r"army", r"navy", r"air force", r"warships?",
         r"frontline", r"front line", r"occupation", r"occupied", r"annex\w*", r"militants?", r"militia\w*",
         r"rebels?", r"insurgen\w*", r"jihadist\w*", r"terror\w*", r"hostages?", r"genocide", r"massacre",
         r"war crimes?", r"nuclear", r"uranium", r"enrichment", r"ballistic", r"weapons", r"arms deal",
         r"arms sales?", r"mobili[sz]ation", r"conscription", r"martial law", r"coup", r"junta",
         r"assassinat\w*", r"espionage", r"spy", r"spies", r"cyberattacks?", r"sabotage", r"blockade",
         r"siege", r"strikes on", r"attacks? on", r"killed in", r"peacekeep\w*", r"security forces"]),
    # diplomacy and states
    (4, [r"sanctions?", r"tariffs?", r"trade war", r"trade deal", r"embargo", r"summit", r"treaty",
         r"accord", r"peace (?:deal|plan|talks|process|agreement|prize)", r"talks", r"negotiat\w*",
         r"diplomat\w*", r"embass(?:y|ies)", r"ambassadors?", r"envoy", r"foreign minister",
         r"foreign secretary", r"secretary of state", r"defen[cs]e minister", r"defen[cs]e secretary",
         r"president\w*", r"prime minister", r"premier", r"chancellor", r"parliament\w*", r"congress",
         r"senate", r"lawmakers", r"legislat\w*", r"government", r"cabinet", r"ministers?", r"ministry",
         r"regime", r"opposition leader", r"opposition", r"sovereign\w*", r"independence", r"separatis\w*",
         r"referendum", r"elections?", r"electoral", r"vote[sd]?", r"voting", r"polls open", r"ballot",
         r"inaugurat\w*", r"impeach\w*", r"resign\w*", r"ousted", r"overthrow\w*", r"crackdown",
         r"dissidents?", r"political prisoners?", r"protests?", r"protesters", r"demonstrat\w*", r"uprising",
         r"unrest", r"riots?", r"curfew", r"state of emergency", r"refugees?", r"asylum", r"migrants?",
         r"displaced", r"humanitarian", r"famine", r"starvation", r"aid", r"blockade", r"border",
         r"borders", r"territor\w*", r"united nations", r"security council",
         r"general assembly", r"european union", r"european commission", r"brics",
         r"asean", r"african union", r"world bank",
         r"international court", r"international criminal court", r"un human rights", r"unhcr", r"unicef",
         r"wfp", r"human rights", r"extradit\w*", r"deport\w*", r"expel\w*", r"detained", r"detention",
         r"nobel peace", r"pentagon", r"kremlin", r"white house", r"downing street", r"elysee",
         r"state department", r"supreme leader", r"head of state", r"foreign policy", r"geopolitic\w*",
         r"alliance", r"allies", r"bloc", r"conflicts?", r"clashes", r"fighting", r"incursions?",
         r"raids?", r"attacks?", r"bombings?", r"deadly"]),
    # economy at state level
    (3, [r"central bank", r"interest rates?", r"rate (?:cut|hike|rise)", r"inflation", r"recession",
         r"gdp", r"economy", r"economic crisis", r"debt crisis", r"default", r"oil prices?", r"gas prices?",
         r"fuel prices?", r"energy crisis", r"oil", r"crude", r"diesel", r"pipeline", r"currency",
         r"devaluation", r"bailout", r"federal reserve", r"the fed", r"ecb", r"bank of england",
         r"bank of japan", r"export controls?", r"rare earths?", r"semiconductors?", r"supply chain"]),
    # disasters (counted as core only with a casualty or scale term, see score())
    (3, [r"earthquake", r"quake", r"tsunami", r"floods?", r"flooding", r"hurricane", r"typhoon",
         r"cyclone", r"landslide", r"mudslide", r"volcan\w*", r"eruption", r"wildfires?", r"drought",
         r"heatwave", r"epidemic", r"pandemic", r"outbreak", r"cholera", r"ebola", r"mpox", r"disaster",
         r"evacuat\w*", r"plane crash", r"air crash", r"shipwreck", r"ferry", r"explosion", r"blast",
         r"collapse"]),
]
DISASTER = _rx(CORE_TERMS[3][1])
CASUALTY = _rx([r"killed", r"kills?", r"killing", r"dead", r"deaths?", r"death toll", r"dies", r"died",
                r"injured", r"wounded", r"missing", r"victims?", r"casualties", r"at least \d+",
                r"hundreds", r"thousands", r"millions?", r"landfall", r"magnitude", r"evacuat\w*",
                r"emergency", r"state of emergency", r"displaced", r"homeless"])
_CORE = [(w, _rx(words)) for w, words in CORE_TERMS[:3]]

LEADERS = _rx([
    r"putin", r"zelensky\w*", r"xi jinping", r"xi", r"trump", r"biden", r"vance", r"rubio", r"hegseth",
    r"netanyahu", r"khamenei", r"pezeshkian", r"erdogan", r"modi", r"macron", r"starmer", r"merz",
    r"scholz", r"meloni", r"sanchez", r"tusk", r"orban", r"fico", r"lula", r"milei", r"sheinbaum",
    r"carney", r"albanese", r"ishiba", r"takaichi", r"lee jae-myung", r"kim jong un", r"kim jong-un",
    r"lukashenko", r"lavrov", r"peskov", r"medvedev", r"assad", r"al-sharaa", r"sharaa", r"mbs",
    r"bin salman", r"al-sisi", r"sisi", r"abbas", r"sinwar", r"guterres", r"von der leyen", r"kallas",
    r"rutte", r"stoltenberg", r"maduro", r"machado", r"ramaphosa", r"tinubu", r"ruto", r"abiy",
    r"burhan", r"hemedti", r"min aung hlaing", r"marcos", r"prabowo", r"anwar", r"sharif", r"munir",
    r"khan", r"yunus", r"hasina", r"lai ching-te", r"wang yi", r"li qiang", r"witkoff", r"kushner",
    r"pope leo", r"pope",
])
HOTSPOTS = _rx([
    r"ukrain\w*", r"russia\w*", r"moscow", r"kyiv", r"gaza\w*", r"israel\w*", r"palestin\w*",
    r"west bank", r"hamas", r"hezbollah", r"houthis?", r"iran\w*", r"tehran", r"china", r"chinese",
    r"beijing", r"taiwan\w*", r"north korea\w*", r"pyongyang", r"syria\w*", r"damascus", r"lebanon",
    r"beirut", r"yemen\w*", r"sudan\w*", r"khartoum", r"darfur", r"rsf", r"sahel", r"mali", r"niger",
    r"burkina faso", r"somalia", r"al-shabab", r"congo", r"drc", r"m23", r"ethiopia\w*", r"eritrea\w*",
    r"tigray", r"myanmar", r"venezuela\w*", r"haiti\w*", r"afghanistan", r"taliban", r"kashmir",
    r"pakistan\w*", r"crimea", r"donbas", r"donetsk", r"zaporizhzhia", r"kharkiv", r"odesa",
    r"red sea", r"strait of hormuz", r"south china sea", r"baltic", r"arctic", r"greenland",
    r"panama canal", r"islamic state", r"al-qaeda", r"wagner",
])
COUNTRIES = _rx([
    r"afghan\w*", r"albania\w*", r"algeria\w*", r"angola\w*", r"argentin\w*", r"armenia\w*",
    r"australia\w*", r"austria\w*", r"azerbaijan\w*", r"bahrain\w*", r"bangladesh\w*", r"belarus\w*",
    r"belgi\w*", r"bolivia\w*", r"bosnia\w*", r"brazil\w*", r"britain", r"british", r"bulgaria\w*",
    r"cambodia\w*", r"cameroon\w*", r"canad\w*", r"chad", r"chile\w*", r"colombia\w*", r"cuba\w*",
    r"cyprus", r"czech\w*", r"denmark", r"danish", r"egypt\w*", r"estonia\w*", r"europe\w*", r"finland",
    r"finnish", r"france", r"french", r"georgia\w*", r"german\w*", r"ghana\w*", r"greece", r"greek",
    r"guatemala\w*", r"guinea\w*", r"hungar\w*", r"india\w*", r"indonesia\w*", r"iraq\w*", r"ireland",
    r"irish", r"italy", r"italian", r"japan\w*", r"jordan\w*", r"kazakh\w*", r"kenya\w*", r"kosovo",
    r"kuwait\w*", r"latvia\w*", r"libya\w*", r"lithuania\w*", r"malaysia\w*", r"mexic\w*",
    r"moldova\w*", r"mongolia\w*", r"montenegro", r"morocc\w*", r"mozambique", r"nepal\w*",
    r"netherlands", r"dutch", r"new zealand", r"nicaragua\w*", r"nigeria\w*", r"norw\w*", r"oman\w*",
    r"panama\w*", r"peru\w*", r"philippine\w*", r"filipino", r"poland", r"polish", r"portug\w*",
    r"qatar\w*", r"romania\w*", r"rwanda\w*", r"saudi\w*", r"senegal\w*", r"serbia\w*", r"singapore\w*",
    r"slovak\w*", r"sloven\w*", r"south africa\w*", r"south korea\w*", r"korea\w*", r"spain", r"spanish",
    r"sri lanka\w*", r"swed\w*", r"swiss", r"switzerland", r"tajik\w*", r"tanzania\w*", r"thai\w*",
    r"tunisia\w*", r"turkey", r"turkish", r"turkiye", r"uganda\w*", r"emirat\w*",
    r"united kingdom", r"united states", r"america\w*", r"uruguay\w*",
    r"uzbek\w*", r"vietnam\w*", r"zambia\w*", r"zimbabwe\w*", r"ecuador\w*", r"el salvador",
    r"honduras", r"dominican", r"jamaica\w*", r"iceland\w*", r"latin america", r"africa\w*",
    r"middle east", r"asia\w*", r"gulf", r"balkans", r"caucasus",
])
QUESTION_PENALTY = 2

# Acronyms count only in capitals ("US" the country, not "us"; "WHO", not "who").
ACRONYMS_CORE = re.compile(r"\b(?:UN|EU|NATO|WHO|ICC|ICJ|IMF|WTO|OPEC\+?|IAEA|UNHCR|UNICEF|WFP|ECB|GDP|G7|G20)\b")
ACRONYMS_PLACE = re.compile(r"(?:\b(?:US|UK|UAE|DRC|RSF|IDF|IRGC|ISIS)\b|\bU\.[SKN]\.)")


@dataclass
class Scored:
    item: Item
    score: int
    core: bool
    reasons: list[str]


def strip_live_prefix(title: str) -> str | None:
    """'Live: X' / 'Ukraine war live: X' -> 'X'. None when what is left is filler."""
    t = title
    m = re.match(r"^(?:[\w' -]{0,40}\blive\b\s*[:|-]\s*)(.+)$", t, re.I)
    if m:
        t = m.group(1)
    t = re.sub(r"\s*[-|]\s*(?:live|latest|latest updates|live updates|as it happened)\s*$", "", t, flags=re.I)
    t = t.strip(" -:|")
    if len(t.split()) < 4 or re.search(r"\b(?:latest|updates)\b\s*$", t, re.I):
        return None
    return t


def _fluff_hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in FLUFF.finditer(text)})


def score_item(item: Item) -> Scored | None:
    """None = hard-excluded. Otherwise a score; keep when score >= MIN_SCORE and core is True."""
    link = item.link.lower()
    if EXCLUDE_URL.search(link):
        return None
    cats = {c.lower() for c in item.categories}
    if cats & EXCLUDE_CATEGORIES:
        return None
    title = strip_live_prefix(item.title)
    if title is None:
        return None
    if EXCLUDE_TITLE.search(title):
        return None
    # NYT briefing bullets end with a period, teasers with "...", and roundups are comma lists.
    if title.endswith(".") or title.count(",") >= 3 or re.search(r"[a-z]\. [A-Z]", title):
        return None
    if SOFT_DROP.search(title):
        return None
    reasons: list[str] = []
    score = 0
    core = False
    for weight, rx in _CORE:
        hits = {m.group(0).lower() for m in rx.finditer(title)}
        if hits:
            score += weight + (1 if len(hits) > 1 else 0)
            core = True
            reasons += sorted(hits)
    dis = {m.group(0).lower() for m in DISASTER.finditer(title)}
    cas = {m.group(0).lower() for m in CASUALTY.finditer(title + " " + item.desc[:200])}
    if dis:
        if cas:
            score += 5
            core = True
            reasons += sorted(dis) + ["+casualty"]
        else:
            score += 1
            reasons += sorted(dis)
    acr = {m.group(0) for m in ACRONYMS_CORE.finditer(title)}
    if acr:
        score += 4
        core = True
        reasons += sorted(acr)
    if ACRONYMS_PLACE.search(title):
        score += 1
        reasons.append("place-acronym")
    lead = {m.group(0).lower() for m in LEADERS.finditer(title)}
    if lead:
        score += 4
        core = True
        reasons += sorted(lead)
    hot = {m.group(0).lower() for m in HOTSPOTS.finditer(title)}
    if hot:
        score += 3
        reasons += sorted(hot)
    ctry = {m.group(0).lower() for m in COUNTRIES.finditer(title)}
    if ctry:
        score += min(2, len(ctry))
        reasons += sorted(ctry)
    # Descriptions confirm the topic (smaller weight); they never rescue a fluff title.
    if item.desc:
        d = item.desc[:300]
        dcore = any(rx.search(d) for _w, rx in _CORE) or bool(LEADERS.search(d))
        if dcore:
            score += 1
        if HOTSPOTS.search(d):
            score += 1
    fl = _fluff_hits(title)
    if fl:
        return None
    if not core and _fluff_hits(item.desc[:300]):
        return None
    crime = {m.group(0).lower() for m in CRIME.finditer(title)}
    if crime:
        # A crime story stays only when the state is the actor or the target.
        statey = bool(LEADERS.search(title)) or any(rx.search(title) for _w, rx in _CORE[:2])
        if not statey:
            return None
        score -= 2
        reasons.append("-crime")
    if SOFT.search(title):
        score -= 2
        reasons.append("-soft")
    if title.rstrip().endswith("?"):
        score -= QUESTION_PENALTY
        reasons.append("-question")
    item = Item(item.source, title, item.link, item.desc, item.published, item.categories)
    return Scored(item, score, core, reasons)


def is_hard_news(item: Item) -> bool:
    s = score_item(item)
    return bool(s and s.core and s.score >= MIN_SCORE)


# ---------------------------------------------------------------- clustering and ranking

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its itself just me more most my no nor not now
of off on once only or other our out over own same she should so some such than that the their them
then there these they this those through to too under until up very was we were what when where which
while who whom why will with would you your says said say new over amid after near amid against
year years day days week weeks month months first last one two three four five six seven eight nine ten
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> set[str]:
    """Significant word stems (first 5 letters), so Russia/Russian and talks/talk meet."""
    out = set()
    for w in _TOKEN_RE.findall(text.lower()):
        if len(w) < 3 or w in STOPWORDS or w.isdigit():
            continue
        out.add(w[:5])
    return out


@dataclass
class Headline:
    key: str
    title: str
    source: str            # source id of the representative headline
    tag: str               # short source tag, e.g. "BBC"
    sources: list[str]     # all outlets carrying the story
    published: float | None
    score: int
    rank: float
    link: str = ""

    @property
    def outlets(self) -> int:
        return len(self.sources)

    def to_dict(self) -> dict:
        return asdict(self)


def _similarity(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    shared = a & b
    if len(shared) < 2:
        return 0.0
    wa = sum(idf.get(t, 1.0) for t in a)
    wb = sum(idf.get(t, 1.0) for t in b)
    ws = sum(idf.get(t, 1.0) for t in shared)
    return ws / max(1e-9, min(wa, wb))


def cluster(scored: list[Scored], threshold: float = 0.34) -> list[list[Scored]]:
    """Link headlines whose IDF-weighted token overlap passes `threshold` (title, plus the first
    sentence of the description at a discount), then take connected components. A link inside one
    outlet needs a closer match (outlets rarely run two headlines on one story)."""
    docs = []
    for s in scored:
        first = re.split(r"(?<=[.!?])\s", s.item.desc, maxsplit=1)[0] if s.item.desc else ""
        docs.append((tokens(s.item.title), tokens(s.item.title + " " + first[:200])))
    df: dict[str, int] = {}
    for _t, full in docs:
        for tok in full:
            df[tok] = df.get(tok, 0) + 1
    n = max(1, len(docs))
    idf = {t: math.log(1 + n / c) for t, c in df.items()}
    parent = list(range(len(scored)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(scored)):
        for j in range(i + 1, len(scored)):
            need = threshold + (0.12 if scored[i].item.source == scored[j].item.source else 0.0)
            sim = max(_similarity(docs[i][0], docs[j][0], idf),
                      0.85 * _similarity(docs[i][1], docs[j][1], idf))
            if sim >= need:
                parent[find(i)] = find(j)
    groups: dict[int, list[Scored]] = {}
    for i in range(len(scored)):
        groups.setdefault(find(i), []).append(scored[i])
    return list(groups.values())


def _representative(members: list[Scored]) -> Scored:
    """The headline most other outlets' headlines overlap with (the core of a chained cluster),
    then plain over question/quote-led/soft titles, higher score, shorter, preferred outlet."""
    toks = [tokens(m.item.title) for m in members]
    central = []
    for i, m in enumerate(members):
        central.append(sum(1 for j, o in enumerate(members)
                           if j != i and o.item.source != m.item.source and len(toks[i] & toks[j]) >= 2))
    best_c = max(central) if central else 0
    members = [m for m, c in zip(members, central) if c >= best_c - 1] or members

    def key(s: Scored):
        t = s.item.title
        length_pen = 0 if len(t) <= 95 else (len(t) - 95) / 10
        q = 1 if t.endswith("?") or t[:1] in "'\"" else 0  # questions and quote-led titles read worse
        soft = 1 if SOFT.search(t) else 0
        pref = PREFERRED.index(s.item.source) if s.item.source in PREFERRED else len(PREFERRED)
        return (q + soft, -s.score + length_pen, pref)
    return min(members, key=key)


def rank_clusters(clusters: list[list[Scored]], now: float, decay_hours: float = DECAY_HOURS) -> list[Headline]:
    out: list[Headline] = []
    for members in clusters:
        rep = _representative(members)
        sources = []
        for s in sorted(members, key=lambda s: PREFERRED.index(s.item.source) if s.item.source in PREFERRED else 99):
            if s.item.source not in sources:
                sources.append(s.item.source)
        dates = [s.item.published for s in members if s.item.published]
        newest = max(dates) if dates else None
        age_h = max(0.0, (now - newest) / 3600) if newest else 6.0
        best = max(s.score for s in members)
        outlets_w = 1.0 + 0.6 * (len(sources) - 1)
        recency = math.exp(-age_h / decay_hours)
        hard = 1.0 + min(best, 20) / 6.0
        rank = outlets_w * recency * hard
        key = "-".join(sorted(tokens(rep.item.title)))[:80] or rep.item.title[:40]
        out.append(Headline(key=key, title=rep.item.title, source=rep.item.source,
                            tag=SOURCES.get(rep.item.source, {}).get("tag", rep.item.source.upper()),
                            sources=sources, published=newest, score=best, rank=round(rank, 4),
                            link=rep.item.link))
    out.sort(key=lambda h: -h.rank)
    return out


def select(items: list[Item], now: float, *, top_n: int = TOP_N, max_age_hours: float = MAX_AGE_HOURS,
           debug: list | None = None) -> list[Headline]:
    """Filter, cluster and rank. `debug` collects (verdict, score, reasons, source, title) rows."""
    kept: list[Scored] = []
    seen_titles: set[tuple[str, str]] = set()
    for it in items:
        if it.published and now - it.published > max_age_hours * 3600:
            if debug is not None:
                debug.append(("old", 0, [], it.source, it.title))
            continue
        k = (it.source, it.title.lower())
        if k in seen_titles:
            continue
        seen_titles.add(k)
        s = score_item(it)
        if s is None:
            if debug is not None:
                debug.append(("drop", 0, [], it.source, it.title))
            continue
        ok = s.core and s.score >= MIN_SCORE
        if debug is not None:
            debug.append(("keep" if ok else "low", s.score, s.reasons, it.source, s.item.title))
        if ok:
            kept.append(s)
    ranked = rank_clusters(cluster(kept), now)
    return ranked[:top_n]


# ---------------------------------------------------------------- cache


class NewsCache:
    """Per-source items on disk. Each source refreshes on its own; a failed source keeps its old items."""

    def __init__(self, cache_dir: str, refresh_seconds: int = REFRESH_SECONDS) -> None:
        self.path = os.path.join(cache_dir, "news_cache.json")
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and isinstance(d.get("sources"), dict):
                return d
        except (OSError, ValueError):
            pass
        return {"sources": {}}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)

    def refresh(self, enabled: list[str], now: float | None = None, force: bool = False) -> dict[str, str]:
        """Fetch the sources that are due. Returns {source: error} for the ones that failed."""
        now = now or time.time()
        errors: dict[str, str] = {}
        with self._lock:
            for sid in enabled:
                meta = SOURCES.get(sid)
                if not meta:
                    continue
                entry = self.data["sources"].get(sid) or {}
                if not force and now - float(entry.get("fetched_at") or 0) < self.refresh_seconds:
                    continue
                try:
                    items = fetch_feed(sid, meta["url"])
                    entry = {"fetched_at": now, "ok": True, "error": None,
                             "items": [asdict(i) for i in items]}
                except (urllib.error.URLError, OSError, ET.ParseError, ValueError, EOFError,
                        zlib.error) as e:
                    msg = f"{type(e).__name__}: {e}"[:200]
                    log.warning("news source %s failed: %s", sid, msg)
                    errors[sid] = msg
                    entry = dict(entry)
                    entry.update({"ok": False, "error": msg, "failed_at": now})
                    # retry sooner than a full refresh, but not every call
                    entry["fetched_at"] = now - self.refresh_seconds + 300
                self.data["sources"][sid] = entry
            self._save()
        return errors

    def items(self, enabled: list[str]) -> list[Item]:
        out = []
        for sid in enabled:
            for d in (self.data["sources"].get(sid) or {}).get("items", []):
                try:
                    out.append(Item(**d))
                except TypeError:
                    continue
        return out

    def status(self) -> dict:
        return {sid: {"ok": e.get("ok"), "error": e.get("error"), "fetched_at": e.get("fetched_at"),
                      "items": len(e.get("items", []))}
                for sid, e in self.data["sources"].items()}


def enabled_sources(settings_sources: dict | None) -> list[str]:
    s = settings_sources or {}
    return [sid for sid, meta in SOURCES.items() if s.get(sid, meta["default"])]


def get_headlines(cache_dir: str, settings_sources: dict | None = None, *, now: float | None = None,
                  force: bool = False, top_n: int = TOP_N) -> dict:
    """Refresh what is due and return {headlines, errors, status, stale}."""
    now = now or time.time()
    cache = NewsCache(cache_dir)
    enabled = enabled_sources(settings_sources)
    errors = cache.refresh(enabled, now, force=force)
    heads = select(cache.items(enabled), now, top_n=top_n)
    newest_fetch = max([float((cache.data["sources"].get(s) or {}).get("fetched_at") or 0)
                        for s in enabled if (cache.data["sources"].get(s) or {}).get("ok")] or [0])
    return {"headlines": heads, "errors": errors, "status": cache.status(),
            "stale": now - newest_fetch > 3 * REFRESH_SECONDS}
