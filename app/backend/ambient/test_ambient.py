"""Unit tests for ambient mode (stdlib unittest, no network, no sign).

    .venv\\Scripts\\python.exe -m unittest app.backend.ambient.test_ambient      (from the repo root)
    .venv\\Scripts\\python.exe app\\backend\\ambient\\test_ambient.py
"""
from __future__ import annotations

import io
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.dirname(APP_DIR), APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PIL import Image  # noqa: E402

from backend.ambient import glyphs as G  # noqa: E402
from backend.ambient import news as N  # noqa: E402
from backend.ambient import rotation as R  # noqa: E402
from backend.ambient import scheduler as SC  # noqa: E402
from backend.ambient import screens as S  # noqa: E402
from backend.ambient import settings as ST  # noqa: E402

TZ = timezone(timedelta(hours=-4))  # a fixed offset: Windows Python has no tzdata, the app uses system local time
PLACE = {"name": "Example City", "lat": 10.0, "lon": 20.0, "timezone": "UTC"}
NOW = time.time()


def item(title, source="bbc", link="https://example.com/news/world-1", desc="", age_h=1.0, cats=None):
    return N.Item(source, title, link, desc, NOW - age_h * 3600, cats or [])


class FilterTests(unittest.TestCase):
    KEEP = [
        "Russian strikes on Ukraine's Zaporizhzhia kill at least seven, including children",
        "Ethiopia warns Eritrea it will defend itself after troops cross border",
        "Powerful magnitude 7.7 earthquake hits Panama, damaging buildings",
        "US imposes sanctions on ICC, hours after former judge Pillay wins Nobel peace prize",
        "India cracks down on youth protests as anger grows against Modi and election chief",
        "Yemen has fallen back into full-scale war, warns UN envoy",
        "Live: Russian strikes on Ukraine's Zaporizhzhia kill at least seven",
        "Taiwan president says defence spending boost aims to 'deter war'",
    ]
    DROP = [
        "West Indies beat India as Hope's hundred secures record T20 chase",
        "A decade of Lajee Celtic: The Palestinian club whose fans include Cantona",
        "Three men found guilty of murdering Australian brothers and US friend in Mexico",
        "'All human emotions': French astronaut Sophie Adenot's first words since returning to Earth",
        "10/9: CBS Evening News",
        "Watch: Moment protesters storm parliament",
        "In pictures: the week in photos",
        "Finland, the world's happiest nation, faces a glum winter",
        "Meet India's feisty Gen Z protester who is taking on Modi",
        "Thieves steal 30,000 bottles of Italian wines in $5.8M heist",
        "Ukraine war live: latest updates",
        "The Nobel Peace Prize recipient is selected in secret by five Norwegians.",
        "How the Houthi-Saudi conflict is affecting countries on Africa's Red Sea coast",
        "Panda diplomacy: China sends two bears to a European zoo",
        "Oscar-winning actor speaks out on the war in Gaza",
    ]

    def test_keeps_hard_news(self):
        for t in self.KEEP:
            with self.subTest(t=t):
                self.assertTrue(N.is_hard_news(item(t)), N.score_item(item(t)))

    def test_drops_fluff(self):
        for t in self.DROP:
            with self.subTest(t=t):
                self.assertFalse(N.is_hard_news(item(t)), N.score_item(item(t)))

    def test_soft_sections_by_url_and_category(self):
        t = "Putin and Trump agree to hold summit on Ukraine war"
        self.assertTrue(N.is_hard_news(item(t)))
        self.assertFalse(N.is_hard_news(item(t, link="https://www.theguardian.com/commentisfree/2026/oct/10/x")))
        self.assertFalse(N.is_hard_news(item(t, link="https://www.bbc.co.uk/sport/football/123")))
        self.assertFalse(N.is_hard_news(item(t, link="https://www.bbc.co.uk/news/av/world-123")))
        self.assertFalse(N.is_hard_news(item(t, cats=["Opinion"])))
        # news clips keep their (hard news) headline; only the headline is ever shown
        self.assertTrue(N.is_hard_news(item(t, link="https://www.france24.com/en/video/20261010-x")))

    def test_acronyms_only_in_capitals(self):
        self.assertFalse(N.is_hard_news(item("Officials defend officers who shot man in same car as child")))
        self.assertTrue(N.is_hard_news(item("WHO declares Ebola outbreak in Congo a global emergency")))

    def test_live_prefix_is_stripped(self):
        s = N.score_item(item("Live: Russian strikes on Ukraine's Zaporizhzhia kill at least seven"))
        self.assertEqual(s.item.title, "Russian strikes on Ukraine's Zaporizhzhia kill at least seven")

    def test_clean_text(self):
        self.assertEqual(N.clean_text("<b>Zelenskyy\u2019s \u201cplan\u201d</b> \u2013 S\u00e3o Paulo &amp; Gda\u0144sk \U0001F600"),
                         "Zelenskyy's \"plan\" - Sao Paulo & Gdansk")


class RankTests(unittest.TestCase):
    def test_clusters_same_story_across_outlets(self):
        items = [
            item("Trump strikes diesel deal with Putin, reversing pressure on Russia over Ukraine war", "f24"),
            item("Trump announces deal for Russian diesel as Zelensky calls it a gift to Putin", "bbc"),
            item("Zelenskyy furious as Trump announces deal to buy Russian diesel", "guardian"),
            item("Ethiopia warns Eritrea it will defend itself after troops cross border", "bbc"),
        ]
        heads = N.select(items, NOW)
        self.assertEqual(len(heads), 2)
        self.assertEqual(heads[0].outlets, 3)
        self.assertIn("diesel", heads[0].title)

    def test_more_outlets_and_newer_rank_higher(self):
        items = [
            item("Ethiopia warns Eritrea it will defend itself after troops cross border", "bbc", age_h=2),
            item("Ethiopia warns Eritrea over troops crossing the border", "dw", age_h=2),
            item("Sudan army retakes Khartoum airport from RSF militia after weeks of fighting", "aj", age_h=2),
        ]
        heads = N.select(items, NOW)
        self.assertIn("Ethiopia", heads[0].title)
        old = [item("Sudan army retakes Khartoum airport from RSF militia after weeks of fighting", "aj", age_h=20),
               item("North Korea fires ballistic missile into the sea, Japan says", "bbc", age_h=1)]
        heads = N.select(old, NOW)
        self.assertIn("North Korea", heads[0].title)

    def test_old_items_are_dropped(self):
        self.assertEqual(N.select([item("North Korea fires ballistic missile into the sea", age_h=50)], NOW), [])

    def test_parse_rss_rdf_and_atom(self):
        rss = b"""<?xml version="1.0"?><rss><channel><item><title>Troops cross border</title>
            <link>https://x/news/1</link><description>&lt;p&gt;War fears&lt;/p&gt;</description>
            <pubDate>Sat, 10 Oct 2026 08:00:00 GMT</pubDate><category>World</category></item></channel></rss>"""
        rdf = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
            <item><title>Summit opens</title><link>https://x/a</link><dc:date>2026-10-10T08:00:00Z</dc:date></item></rdf:RDF>"""
        atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Talks resume</title>
            <link href="https://x/b"/><updated>2026-10-10T08:00:00Z</updated></entry></feed>"""
        for data, title in ((rss, "Troops cross border"), (rdf, "Summit opens"), (atom, "Talks resume")):
            items = N.parse_feed("bbc", data)
            self.assertEqual(items[0].title, title)
            self.assertIsNotNone(items[0].published)
        self.assertEqual(N.parse_feed("bbc", rss)[0].desc, "War fears")


class SchedulerTests(unittest.TestCase):
    cfg = ST.validate({"location": PLACE})

    def at(self, hh, mm, ss=0):
        return datetime(2026, 10, 10, hh, mm, ss, tzinfo=TZ)

    def test_night_uploads_once(self):
        st = SC.SchedState()
        d = SC.decide(self.at(5, 0), self.cfg, st)
        self.assertEqual((d.mode, d.action), ("night", "night"))
        SC.record_upload(st, "night", True, self.at(5, 0).timestamp())
        d = SC.decide(self.at(5, 2), self.cfg, st)
        self.assertEqual((d.mode, d.action), ("night", "none"))
        self.assertEqual(d.next_upload, self.at(8, 0).timestamp())
        d = SC.decide(self.at(7, 59, 59), self.cfg, st)
        self.assertEqual(d.action, "none")

    def test_day_starts_at_8_and_refreshes(self):
        st = SC.SchedState(last_kind="night", last_upload_at=self.at(5, 0).timestamp(), last_attempt_at=self.at(5, 0).timestamp())
        d = SC.decide(self.at(8, 0), self.cfg, st)
        self.assertEqual((d.mode, d.action), ("day", "day"))
        SC.record_upload(st, "day", True, self.at(8, 0).timestamp())
        self.assertEqual(SC.decide(self.at(8, 4, 59), self.cfg, st).action, "none")
        self.assertEqual(SC.decide(self.at(8, 5), self.cfg, st).action, "day")

    def test_22_00_switches_to_night(self):
        st = SC.SchedState(last_kind="day", last_upload_at=self.at(21, 58).timestamp(), last_attempt_at=self.at(21, 58).timestamp())
        self.assertEqual(SC.decide(self.at(21, 59, 59), self.cfg, st).mode, "day")
        d = SC.decide(self.at(22, 0), self.cfg, st)
        self.assertEqual((d.mode, d.action), ("night", "night"))

    def test_failure_retries_after_a_minute(self):
        st = SC.SchedState(last_kind="night")
        SC.record_upload(st, "day", False, self.at(8, 0).timestamp())
        self.assertEqual(SC.decide(self.at(8, 0, 30), self.cfg, st).action, "none")
        self.assertEqual(SC.decide(self.at(8, 1), self.cfg, st).action, "day")

    def test_pause_and_resume(self):
        st = SC.SchedState(last_kind="day", last_upload_at=self.at(9, 0).timestamp(), last_attempt_at=self.at(9, 0).timestamp())
        SC.record_manual(st, self.at(9, 1).timestamp(), 30)
        d = SC.decide(self.at(9, 20), self.cfg, st)
        self.assertEqual((d.mode, d.action), ("paused", "none"))
        d = SC.decide(self.at(9, 31), self.cfg, st)
        self.assertEqual((d.mode, d.action), ("day", "day"))
        SC.pause(st, self.at(10, 0).timestamp(), 60)
        self.assertEqual(SC.decide(self.at(10, 30), self.cfg, st).mode, "paused")
        SC.resume(st)
        self.assertEqual(SC.decide(self.at(10, 30), self.cfg, st).action, "day")

    def test_next_upload_follows_the_rotation_on_the_sign(self):
        st = SC.SchedState(last_kind="night")
        SC.record_upload(st, "day", True, self.at(9, 0).timestamp())
        self.assertIsNone(st.next_due_at)
        st.next_due_at = self.at(9, 3, 40).timestamp()       # set by the service from the handover window
        self.assertEqual(SC.decide(self.at(9, 3, 39), self.cfg, st).action, "none")
        d = SC.decide(self.at(9, 3, 40), self.cfg, st)
        self.assertEqual((d.action, d.reason), ("day", "refresh"))
        SC.record_upload(st, "day", False, self.at(9, 3, 40).timestamp())
        self.assertEqual(SC.decide(self.at(9, 4, 0), self.cfg, st).action, "none")   # retry after a minute
        self.assertEqual(SC.decide(self.at(9, 4, 41), self.cfg, st).action, "day")
        # news alone hands over about every 75 s: the prep 45 s after an upload must not be held back
        SC.record_upload(st, "day", True, self.at(9, 5, 0).timestamp())
        st.next_due_at = self.at(9, 5, 45).timestamp()
        self.assertEqual(SC.decide(self.at(9, 5, 45), self.cfg, st).action, "day")

    def test_disabled_and_wrapping_window(self):
        self.assertEqual(SC.decide(self.at(12, 0), ST.validate({"enabled": False, "location": PLACE}),
                                   SC.SchedState()).mode, "off")
        cfg = ST.validate({"active_start": "20:00", "active_end": "02:00", "location": PLACE})
        self.assertEqual(SC.decide(self.at(23, 0), cfg, SC.SchedState()).mode, "day")
        self.assertEqual(SC.decide(self.at(1, 59), cfg, SC.SchedState()).mode, "day")
        self.assertEqual(SC.decide(self.at(2, 0), cfg, SC.SchedState()).mode, "night")

    def test_off_until_a_location_is_set(self):
        cfg = ST.validate({})
        self.assertIsNone(cfg["location"])
        d = SC.decide(self.at(12, 0), cfg, SC.SchedState())
        self.assertEqual((d.mode, d.action, d.reason), ("off", "none", SC.NO_LOCATION))
        d = SC.decide(self.at(3, 0), cfg, SC.SchedState())
        self.assertEqual((d.mode, d.action), ("off", "none"))
        for bad in ({"name": "x"}, {"lat": "a", "lon": 1}, "Somewhere", []):
            self.assertIsNone(ST.validate({"location": bad})["location"], bad)
        loc = ST.validate({"location": {"lat": "12.5", "lon": 200}})["location"]
        self.assertEqual((loc["lat"], loc["lon"], loc["timezone"]), (12.5, 180.0, "auto"))

    def test_locale_defaults(self):
        units, clock_24h = ST.locale_defaults()
        self.assertIn(units, ("C", "F"))
        self.assertIsInstance(clock_24h, bool)
        s = ST.validate({})
        self.assertEqual((s["units"], s["clock_24h"]), (units, clock_24h))
        self.assertEqual(ST.validate({"units": "c"})["units"], "C")
        self.assertEqual(ST.validate({"units": "kelvin"})["units"], units)

    def test_settings_validation(self):
        s = ST.validate({"refresh_minutes": 99, "active_start": "25:00", "screens": {"clock": {"seconds": 1}},
                         "day_color_scale": 3, "news_sources": {"bbc": False}, "junk": 1})
        self.assertEqual(s["refresh_minutes"], 15)
        self.assertEqual(s["active_start"], "08:00")
        self.assertEqual(s["screens"]["clock"]["seconds"], 10)
        self.assertEqual(s["day_color_scale"], 1.0)
        self.assertFalse(s["news_sources"]["bbc"])
        self.assertNotIn("junk", s)
        self.assertEqual(s["night_text"], "")


DRONE = "Drone strike by Sudan paramilitary kills 41 near Ethiopian border, rights group says"
LONG = ("UN Security Council holds emergency session as ceasefire talks between the warring parties collapse "
        "for the third time in a week, raising fears of a wider regional war across the Horn of Africa, the "
        "Red Sea and the Gulf as shipping insurers pull cover and aid agencies warn of famine in the camps")


def headline(i, title=None, source="bbc"):
    return N.Headline(key=f"k{i}", title=title or f"Headline number {i} about troops at the border talks",
                      source=source, tag=source.upper(), sources=[source], published=NOW - 3600, score=10, rank=10 - i)


def gif_frames(gif: bytes) -> list[tuple[Image.Image, int]]:
    im = Image.open(io.BytesIO(gif))
    out = []
    for i in range(im.n_frames):
        im.seek(i)
        out.append((im.convert("RGB"), im.info["duration"]))
    return out


def seg_at(rot, planned: datetime):
    """The segment of a rotation playing at a planned moment (None in the tail)."""
    for s in rot.segments:
        if s.start <= planned < s.end:
            return s
    return None


def item_spans(seg, motion):
    """(start, end) of every news item in a segment, as planned."""
    t = seg.start
    out = []
    for it in seg.items:
        e = t + timedelta(milliseconds=S.item_ms(it, motion))
        out.append((t, e, it))
        t = e
    return out


def real_heads():
    titles = ["Ukraine warns Trump-Putin diesel deal will bankroll Russia's war",
              "Indian Police Round Up Protest Leaders and Lock Down Delhi",
              "Three Saudis killed in Riyadh airport attack claimed by Houthis",
              "Nobel peace laureate says US sanctions on ICC 'unacceptable'",
              "Abbas postpones Palestinian legislative elections to September 2027",
              "Yemen has fallen back into full-scale war, warns UN envoy",
              "Eritrean troops in Ethiopia raise fears of a regional war",
              "Woman injures three people in third Poland school knife attack in a week",
              "Russian glide bomb attack on Zaporizhzhia kills at least 15 people",
              DRONE]
    hs = [headline(i, t) for i, t in enumerate(titles)]
    hs[-1].rank = 100   # shown first
    return hs


class NewsTests(unittest.TestCase):
    def test_speed_presets_are_smooth_and_about_twice_as_fast(self):
        old = 1000 / 60   # what the sign scrolled at before (2 px every 120 ms in news-only)
        m = S.news_motion("normal")
        self.assertEqual((m.step, m.ms), (2, 60))
        self.assertGreaterEqual(m.px_per_s / old, 1.75)
        self.assertLessEqual(m.px_per_s / old, 2.05)
        self.assertEqual(S.news_motion(ST.validate({})["news_speed"]), m)   # the default
        self.assertEqual((S.news_motion("slow").step, S.news_motion("slow").ms), (1, 50))
        self.assertAlmostEqual(S.news_motion("fast").px_per_s, 50)
        self.assertAlmostEqual(S.news_motion(40).px_per_s, 40)
        for v in ("slow", "normal", "fast", 10, 18, 25, 30, 33, 40, 45, 60, 75, 500, -3):
            mm = S.news_motion(v)
            self.assertIn(mm.step, (1, 2, 3))
            if mm.step > 1:
                self.assertTrue(40 <= mm.ms <= 60, (v, mm))
            else:
                self.assertGreaterEqual(mm.ms, 40)

    def test_news_speed_setting(self):
        for given, want in (("fast", "fast"), (" SLOW ", "slow"), (40, 40), ("45", 45), (33.33, 33.3),
                            (500, 75), (2, 10), ("junk", "normal"), (True, "normal"), (None, "normal")):
            with self.subTest(given=given):
                self.assertEqual(ST.validate({"news_speed": given})["news_speed"], want)

    def test_headline_scrolls_fully_in_and_out(self):
        m = S.news_motion("normal")
        h = headline(0, DRONE)
        it = S.NewsItem(h, S.item_title(h))
        self.assertEqual(it.title, DRONE)                 # never cut
        frames = S.item_frames(it, m, NOW)
        self.assertEqual(sum(ms for _i, ms in frames), S.item_ms(it, m))
        rows = [img.crop((0, S.TEXT_Y, 64, S.TEXT_Y + 8)) for img, _ms in frames]
        self.assertIsNone(rows[0].getbbox())              # the gap: the next tag over an empty row
        first = rows[1].getbbox()
        self.assertIsNotNone(first)
        self.assertGreaterEqual(first[0], 64 - m.step)    # enters at the right edge
        self.assertIsNone(rows[-1].getbbox())             # and has left at the left edge
        self.assertIsNotNone(rows[-2].getbbox())
        self.assertTrue(all(ms == m.ms for _i, ms in frames[1:]))
        # every column of the title passes the screen: the left edge of the text moves a step a frame
        tw = G.text_width(DRONE, G.FONT_5X7)
        self.assertGreaterEqual((len(frames) - 1) * m.step, 64 + tw)
        held = S.NewsItem(h, it.title, hold_ms=5000)
        hf = S.item_frames(held, m, NOW)
        self.assertEqual(sum(ms for _i, ms in hf), S.item_ms(it, m) + 5000)
        k = S.hold_index(m)
        self.assertEqual(hf[k][1], m.ms + 5000)           # the hold frame shows the title's start
        self.assertLessEqual(hf[k][0].crop((0, S.TEXT_Y, 64, S.TEXT_Y + 8)).getbbox()[0], 1)

    def test_very_long_headline_is_cut_at_a_word_with_an_ellipsis(self):
        t = S.fit_title(LONG)
        self.assertLessEqual(G.text_width(t, G.FONT_5X7), S.MAX_TITLE_PX)
        self.assertTrue(t.endswith("..."))
        self.assertTrue(LONG.startswith(t[:-3]))
        self.assertIn(LONG[len(t) - 3], " ,")            # cut where a word ended
        self.assertEqual(S.fit_title(DRONE), DRONE)
        mid = "Talks " * 25 + "end"                      # about 150 characters: shown in full
        self.assertEqual(S.fit_title(mid.strip()), mid.strip())

    def test_plan_news_takes_whole_headlines(self):
        m = S.news_motion("normal")
        heads = [headline(i) for i in range(6)]
        one = S.item_ms(S.NewsItem(None, heads[0].title), m)
        items, total = S.plan_news(heads, 2 * one + 100, m)
        self.assertEqual(len(items), 2)
        self.assertEqual(total, 2 * one)
        items, total = S.plan_news(heads, 3000, m)       # a slot shorter than any headline
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, heads[0].title)
        self.assertEqual(total, one)


class RotationTests(unittest.TestCase):
    cfg = ST.validate({})
    news_only = ST.validate({"screens": {"clock": {"on": False}, "now": {"on": False}, "today": {"on": False},
                                         "week": {"on": False}}})

    def heads(self, n=6):
        return [headline(i) for i in range(n)]

    def assert_valid(self, rot_or_segs, motion=None):
        segs = rot_or_segs.segments if hasattr(rot_or_segs, "segments") else rot_or_segs
        motion = motion or S.news_motion()
        t = segs[0].start
        for s in segs:
            self.assertEqual(s.start, t)                  # contiguous
            self.assertEqual(s.ms % 10, 0)                # centisecond GIF delays
            t = s.end
            if s.kind == "clock":
                self.assertGreaterEqual(s.start, s.minute)                       # never early
                self.assertLessEqual(s.end, s.minute + timedelta(minutes=1))   # never past its minute
            if s.kind == "news":
                self.assertTrue(s.items)
                need = sum(S.item_ms(it, motion) for it in s.items)
                self.assertGreaterEqual(s.ms, need)       # every headline fits whole
                for it in s.items:
                    self.assertEqual(it.title, S.fit_title(it.headline.title if hasattr(it.headline, "title")
                                                           else it.headline["title"]))

    def test_segments_keep_the_clock_inside_its_minute(self):
        start = datetime(2026, 10, 10, 8, 0, 17, 340000, tzinfo=TZ)
        segs, _ = R.plan_segments(start, 7, kinds=R.DEFAULT_CYCLE, headlines=real_heads())
        self.assert_valid(segs)
        for s in segs:
            if s.kind == "clock" and s.start != segs[0].start:
                self.assertGreaterEqual(s.start, s.minute + timedelta(milliseconds=R.CLOCK_EARLY_MS))
                self.assertLessEqual(s.end, s.minute + timedelta(minutes=1) - timedelta(milliseconds=R.CLOCK_LATE_MS))
        total = sum(s.ms for s in segs) / 1000
        self.assertGreaterEqual(total, 7 * 60)
        self.assertLess(total, 8 * 60 + 5)
        others = [s.kind for s in segs if s.kind != "clock"]
        self.assertEqual(others[:5], ["now", "today", "week", "news", "news"])

    def test_long_clock_setting_still_leaves_room_for_other_screens(self):
        start = datetime(2026, 10, 10, 8, 0, 10, tzinfo=TZ)
        segs, _ = R.plan_segments(start, 5, clock_seconds=50, kinds=R.DEFAULT_CYCLE, headlines=real_heads())
        self.assert_valid(segs)
        self.assertGreaterEqual(len([s for s in segs if s.kind != "clock"]), 4)

    def test_cycle_continues_across_uploads(self):
        start = datetime(2026, 10, 10, 8, 0, 30, tzinfo=TZ)
        segs, idx = R.plan_segments(start, 7, kinds=R.DEFAULT_CYCLE, headlines=self.heads())
        n_other = len([s for s in segs if s.kind != "clock"])
        self.assertEqual(idx, n_other % 5)
        segs2, _ = R.plan_segments(start + timedelta(minutes=5), 7, kinds=R.DEFAULT_CYCLE, cycle_index=idx,
                                   headlines=self.heads())
        first_other = next(s.kind for s in segs2 if s.kind != "clock")
        self.assertEqual(first_other, R.DEFAULT_CYCLE[idx])

    def test_clock_only_switches_on_the_minute(self):
        start = datetime(2026, 10, 10, 8, 0, 40, tzinfo=TZ)
        segs, _ = R.plan_segments(start, 3, kinds=[])
        self.assert_valid(segs)
        self.assertEqual({s.kind for s in segs}, {"clock"})
        self.assertGreaterEqual(len({s.minute for s in segs}), 3)

    def test_short_first_slot_never_cuts_the_drone_headline(self):
        # What happened at 14:07: news only, the GIF planned to start 5 s before the next minute. The
        # old layout gave the first headline a 5 s slot and cut it to "Drone strike by...".
        start = datetime(2026, 10, 10, 14, 7, 57, tzinfo=TZ)
        for cfg in (self.news_only, self.cfg):
            state = R.RotationState()
            rot = R.build_rotation(start - timedelta(seconds=40), cfg, R.fake_weather(), real_heads(), state,
                                   start=start)
            self.assert_valid(rot)
            news = [s for s in rot.segments if s.kind == "news"]
            titles = [it.title for s in news for it in s.items]
            self.assertIn(DRONE, titles)
            self.assertFalse(any(t.endswith("...") for t in titles))
        self.assertEqual(rot.segments[0].kind, "clock")    # with the clock on, a GIF starts on the clock

    def test_very_long_headline_in_a_short_slot(self):
        heads = [headline(0, LONG)] + self.heads(3)
        heads[0].rank = 100
        start = datetime(2026, 10, 10, 9, 0, 10, tzinfo=TZ)
        for secs in (10, 25, 50):
            segs, _ = R.plan_segments(start, 6, clock_seconds=secs, kinds=["news"], headlines=heads)
            self.assert_valid(segs)
            first = next(s for s in segs if s.kind == "news")
            self.assertEqual(first.items[0].title, S.fit_title(LONG))
            self.assertTrue(first.items[0].title.endswith("..."))
        segs, _ = R.plan_segments(start, 6, clock_on=False, kinds=["news"], headlines=heads)
        self.assert_valid(segs)

    def test_gif_matches_plan_and_is_small(self):
        state = R.RotationState()
        wx = R.fake_weather(code=61)
        now = datetime(2026, 10, 10, 9, 0, 0, tzinfo=TZ)
        rot = R.build_rotation(now, self.cfg, wx, self.heads(), state)
        frames = gif_frames(rot.gif)
        self.assertTrue(all(ms >= R.MIN_FRAME_MS for _i, ms in frames))   # the sign plays none faster
        self.assertEqual(sum(ms for _i, ms in frames), rot.total_ms + rot.tail_ms)
        self.assertEqual(rot.tail_ms, R.TAIL_MS)
        self.assertLess(len(rot.gif), 200_000)
        self.assertGreater(rot.start, now)
        self.assertEqual(frames[-1][1], R.TAIL_MS)        # the still weather tail, no clock
        self.assertLessEqual(max(frames[0][0].tobytes()), int(255 * 0.7 + 0.5))   # day color scale
        self.assertEqual(rot.segments[0].kind, "clock")
        self.assertTrue(rot.segments[-1].primary and rot.segments[-1].kind == "clock")
        self.assertEqual(state.windows, rot.windows)
        self.assertEqual(state.plan_start, rot.start.timestamp())

    def test_news_frames_match_the_motion(self):
        state = R.RotationState()
        cfg = dict(self.news_only, day_color_scale=1.0)
        rot = R.build_rotation(datetime(2026, 10, 10, 9, 0, 0, tzinfo=TZ), cfg, None, real_heads(), state)
        m = rot.motion
        scroll = [ms for _i, ms in gif_frames(rot.gif)[:-1] if ms == m.ms]
        self.assertGreater(len(scroll), 1000)
        self.assertAlmostEqual(m.px_per_s, 33.3, places=1)

    def test_max_frame_ms_splits_long_stills(self):
        cfg = ST.validate({"max_frame_ms": 5000})
        rot = R.build_rotation(datetime(2026, 10, 10, 9, 0, 0, tzinfo=TZ), cfg, R.fake_weather(code=3), self.heads(),
                               R.RotationState())
        frames = gif_frames(rot.gif)
        self.assertLessEqual(max(ms for _i, ms in frames), 5000)
        self.assertEqual(sum(ms for _i, ms in frames), rot.total_ms + rot.tail_ms)

    def test_news_rotates_through_headlines(self):
        state = R.RotationState()
        heads = self.heads(9)
        start = datetime(2026, 10, 10, 9, 0, 0, tzinfo=TZ)
        rot = R.build_rotation(start, self.news_only, None, heads, state, start=start)
        keys = [it.headline.key for s in rot.segments for it in s.items]
        self.assertEqual(len(set(keys[:9])), 9)          # no repeats before the list is used up
        self.assertEqual(len(state.news_shown), 9)
        oldest = min(state.news_shown, key=state.news_shown.get)
        later = start + timedelta(minutes=5)
        rot2 = R.build_rotation(later, self.news_only, None, heads, state, start=later)
        self.assertEqual(rot2.segments[0].items[0].headline.key, oldest)

    def test_night_frame_is_black_by_default(self):
        self.assertEqual(S.night_frame("").getbbox(), None)
        img = S.night_frame("gn :)")
        self.assertLessEqual(max(img.tobytes()), 12)

    def test_screens_fill_their_duration(self):
        wx = R.fake_weather(temp=-5, hi=102, lo=-12, code=95)
        m = datetime(2026, 10, 10, 10, 58)
        mo = S.news_motion()
        items = [S.NewsItem(h, S.item_title(h)) for h in self.heads(2)]
        need = sum(S.item_ms(it, mo) for it in items)
        for fr in (S.clock_frames(m, wx, 25000), S.now_frames(wx, 35000), S.today_frames(wx, 35000),
                   S.week_frames(wx, 35000), S.news_frames(items, need + 990, mo)):
            self.assertIn(sum(ms for _i, ms in fr), (25000, 35000, need + 990))
            for img, _ms in fr:
                self.assertEqual(img.size, (64, 16))
        with self.assertRaises(ValueError):
            S.news_frames(items, need - 10, mo)


class HandoverTests(unittest.TestCase):
    """The next upload completes inside the old GIF's handover window and continues it."""
    cfg = ST.validate({})
    news_only = RotationTests.news_only

    def span(self, cfg):
        return None if cfg["screens"]["clock"]["on"] else 100

    def first(self, cfg, start, heads=None, drift=1.5, payload=150_000):
        for _attempt in range(2):   # as the service does: size the hold for this GIF's own upload time
            state = R.RotationState(bytes_per_second=9000.0, last_payload_bytes=payload)
            rot = R.build_rotation(start - timedelta(seconds=25), cfg, R.fake_weather(), heads or real_heads(),
                                   state, start=start, span_s=self.span(cfg))
            payload = rot.payload_bytes
        state.drift = drift
        return state, rot

    def playing(self, old, old_drift, new, switch_at: datetime, real: datetime):
        """(rotation, segment) on the sign at a real moment: the old GIF until the switch, then the new
        one (which starts its frame 0 at the switch)."""
        if real < switch_at:
            return old, seg_at(old, real - timedelta(seconds=old_drift))
        return new, seg_at(new, new.start + (real - switch_at))

    def check_switch(self, cfg, *, drift=1.5, late_s=(-1.0, 0.0, 2.0, 4.0), force=False, now_offset=None):
        start = datetime(2026, 10, 10, 9, 0, 20, tzinfo=TZ)
        state, old = self.first(cfg, start, drift=drift)
        est = state.estimate_upload(old.payload_bytes)
        now = (old.start + timedelta(seconds=now_offset)) if now_offset is not None else \
            datetime.fromtimestamp(old.primary["w0"]).astimezone() - timedelta(seconds=est + 20)
        pick = R.choose_switch(state, now.timestamp(), now.timestamp() + 1 + est, est, force=force)
        self.assertIsNotNone(pick)
        at = datetime.fromtimestamp(pick["at"]).astimezone()
        st2 = R.RotationState.from_dict(state.to_dict())
        new = R.build_rotation(now, cfg, R.fake_weather(), real_heads(), st2, start=at,
                               cont=pick["cont"], span_s=self.span(cfg))
        motion = new.motion
        for late in late_s:                              # the upload finishes this much later than planned
            switch = at + timedelta(seconds=late)
            rot, seg = self.playing(old, drift, new, switch, switch - timedelta(milliseconds=1))
            self.assertIs(rot, old)
            self.assertIsNotNone(seg, "the old GIF still has content at the switch")
            if cfg["screens"]["clock"]["on"]:
                self.assertEqual(seg.kind, "clock")      # lands on a clock, never mid-headline
                self.assertEqual(new.segments[0].kind, "clock")
            else:
                self.assertEqual(seg.kind, "news")
                old_item = next(it for a, e, it in item_spans(seg, motion)
                                if a <= switch - timedelta(seconds=drift) < e)
                self.assertEqual(old_item.headline.key if hasattr(old_item.headline, "key") else old_item.headline["key"],
                                 pick["cont"]["headline"]["key"])
                first = new.segments[0].items[0]
                self.assertTrue(first.from_hold)
                self.assertEqual(first.headline["key"], pick["cont"]["headline"]["key"])
            # the clock is right at every moment around the switch
            t = switch - timedelta(seconds=30)
            while t < switch + timedelta(seconds=90):
                _r, s = self.playing(old, drift, new, switch, t)
                if s is not None and s.kind == "clock":
                    self.assertEqual(s.minute, _floor(t), f"late {late}: {t} shows {s.minute}")
                t += timedelta(milliseconds=250)
        return pick, old, new

    def test_scheduled_switch_lands_on_the_last_clock(self):
        pick, old, new = self.check_switch(self.cfg)
        self.assertTrue(pick["window"]["primary"])
        self.assertEqual(pick["cont"], {"kind": "clock"})

    def test_scheduled_switch_with_the_old_gif_running_late(self):
        self.check_switch(self.cfg, drift=7.0, late_s=(-2.0, 0.0, 4.0))
        self.check_switch(self.cfg, drift=-2.0, late_s=(-2.0, 0.0, 4.0))

    def test_upload_landing_mid_rotation_waits_for_a_clock(self):
        # "Run now" in the middle of a rotation: the switch still lands on a clock, within a minute.
        for off in (40, 95, 130, 200):
            pick, old, _new = self.check_switch(self.cfg, force=True, now_offset=off, late_s=(-1.0, 0.0, 3.0))
            self.assertFalse(pick["window"]["primary"] and off < 200)
            self.assertLessEqual(pick["at"] - (old.start.timestamp() + off), R.FORCE_WAIT_MAX)

    def test_news_only_switch_lands_on_the_held_headline(self):
        pick, old, new = self.check_switch(self.news_only)
        self.assertEqual(pick["kind"], "news")
        self.assertTrue(new.segments[0].cont)
        # the new GIF picks up the rest of the cycle without repeating the held headline at once
        keys = [it.headline.key if hasattr(it.headline, "key") else it.headline["key"]
                for it in new.segments[0].items]
        self.assertEqual(len(keys), len(set(keys)))

    def test_news_only_content_follows_the_span(self):
        # the service shortens a big rotation; the last block of headlines must shrink with it
        start = datetime(2026, 10, 10, 9, 0, 20, tzinfo=TZ)
        for span in (60, 75, 150, 300):
            rot = R.build_rotation(start, self.news_only, None, real_heads(), R.RotationState(), start=start,
                                   span_s=span)
            w0 = rot.primary["w0"] - start.timestamp()
            self.assertGreaterEqual(w0, span - 1)
            self.assertLess(w0, span + 25, span)

    def test_missed_window_lands_on_the_still_tail(self):
        start = datetime(2026, 10, 10, 9, 0, 20, tzinfo=TZ)
        state, old = self.first(self.news_only, start)
        late = old.primary["w1"] + 60
        pick = R.choose_switch(state, late - 30, late, 10.0)
        self.assertEqual(pick["kind"], "tail")
        self.assertIsNone(pick["cont"])
        self.assertIsNone(seg_at(old, datetime.fromtimestamp(pick["at"] - state.drift).astimezone()))
        state.drift = None                                   # unknown: start fresh
        self.assertIsNone(R.choose_switch(state, late - 30, late, 10.0))

    def test_safe_clock_start(self):
        m = datetime(2026, 10, 10, 9, 0, tzinfo=TZ)
        self.assertEqual(R.safe_clock_start(m + timedelta(seconds=1)), m + timedelta(seconds=4))
        self.assertEqual(R.safe_clock_start(m + timedelta(seconds=30)), m + timedelta(seconds=30))
        self.assertEqual(R.safe_clock_start(m + timedelta(seconds=50)), m + timedelta(minutes=1, seconds=4))


class LoopTests(unittest.TestCase):
    """Clock off: a GIF of whole headlines that the sign loops by itself, uploaded every refresh."""
    news_only = RotationTests.news_only
    start = datetime(2026, 10, 10, 9, 0, 20, tzinfo=TZ)

    def text_row(self, img):
        return img.crop((0, S.TEXT_Y, 64, S.TEXT_Y + 8)).getbbox()

    def test_loop_wraps_cleanly_with_whole_headlines(self):
        rot = R.build_loop(self.start, self.news_only, None, real_heads(), R.RotationState(), start=self.start)
        self.assertTrue(rot.loop)
        self.assertLessEqual(rot.payload_bytes, 160_000)
        frames = gif_frames(rot.gif)
        self.assertEqual(sum(ms for _i, ms in frames), rot.total_ms)    # no tail: it loops
        self.assertEqual(rot.tail_ms, 0)
        self.assertEqual(Image.open(io.BytesIO(rot.gif)).info.get("loop"), 0)
        self.assertIsNone(self.text_row(frames[0][0]))                  # the wrap: empty row to empty row
        self.assertIsNone(self.text_row(frames[-1][0]))
        items = [it for s in rot.segments for it in s.items]
        self.assertGreaterEqual(len(items), 3)
        for it in items:
            self.assertEqual(it.title, S.fit_title(it.headline.title))
        self.assertEqual(items[0].gap_ms, R.LOOP_GAP_MS)
        self.assertTrue(all(it.gap_ms == S.NEWS_GAP for it in items[1:]))
        w = rot.primary
        self.assertEqual((w["kind"], w["w0"]), ("loop", self.start.timestamp()))
        self.assertAlmostEqual(w["period"], rot.total_ms / 1000)

    def test_budget_sets_the_number_of_headlines(self):
        big = R.build_loop(self.start, self.news_only, None, real_heads(), R.RotationState(), start=self.start)
        small = R.build_loop(self.start, self.news_only, None, real_heads(), R.RotationState(), start=self.start,
                             budget_bytes=60_000)
        n_big = sum(len(s.items) for s in big.segments)
        n_small = sum(len(s.items) for s in small.segments)
        self.assertLess(n_small, n_big)
        self.assertLessEqual(small.payload_bytes, 60_000)

    def test_sets_rotate_through_every_headline(self):
        st = R.RotationState()
        sets = []
        for k in range(3):
            t = self.start + timedelta(minutes=5 * k)
            rot = R.build_loop(t, self.news_only, None, real_heads(), st, start=t)
            sets.append({it.headline.key for s in rot.segments for it in s.items})
        self.assertFalse(sets[0] & sets[1] and len(sets[0]) + len(sets[1]) <= 10)
        self.assertEqual(len(sets[0] | sets[1] | sets[2]), 10)

    def test_next_switch_waits_a_refresh_and_lands_in_the_wrap_gap(self):
        st = R.RotationState()
        rot = R.build_loop(self.start, self.news_only, None, real_heads(), st, start=self.start)
        st.drift = 0.8
        period = rot.total_ms / 1000
        now = self.start.timestamp() + 30
        pick = R.choose_switch(st, now, now + 20, 18.0)
        self.assertEqual(pick["kind"], "loop")
        self.assertGreaterEqual(pick["at"] - (self.start.timestamp() + st.drift), 5 * 60)   # refresh_minutes
        self.assertLess(pick["at"] - (self.start.timestamp() + st.drift), 5 * 60 + period)
        frames = gif_frames(rot.gif)
        for late in (-0.5, 0.0, 0.5):                     # the old loop is between headlines there
            off = int(((pick["at"] + late - st.drift - self.start.timestamp()) % period) * 1000)
            acc = 0
            for img, ms in frames:
                if acc + ms > off:
                    self.assertIsNone(self.text_row(img), f"late {late}")
                    break
                acc += ms
        self.assertAlmostEqual(pick["later"][0] - pick["at"], period, places=3)   # a slow link takes the next wrap
        forced = R.choose_switch(st, now, now + 20, 18.0, force=True)
        self.assertLessEqual(forced["at"] - now, period + 20)
        # a loop never takes over on a held headline of a non-loop GIF: only between headlines
        st2, old = HandoverTests().first(self.news_only, self.start)
        pick = R.choose_switch(st2, self.start.timestamp() + 60, self.start.timestamp() + 80, 18.0,
                               allow={"loop", "tail", "now", "today", "week"})
        self.assertEqual(pick["kind"], "tail")

    def test_clock_rotation_never_hands_over_sooner_than_refresh(self):
        for refresh in (3, 5, 9):
            st = R.RotationState()
            cfg = ST.validate({"refresh_minutes": refresh})
            rot = R.build_rotation(self.start, cfg, R.fake_weather(), real_heads(), st, start=self.start)
            st.drift = 0.0
            self.assertGreaterEqual(rot.primary["w0"] - self.start.timestamp(), refresh * 60)
            pick = R.choose_switch(st, self.start.timestamp(), self.start.timestamp(), 18.0)
            self.assertGreaterEqual(pick["at"] - self.start.timestamp(), refresh * 60)


class PacingTests(unittest.TestCase):
    """A timed upload starts early and spreads short pauses between chunks to complete on time."""

    def run_upload(self, payload_len: int, finish_in: float | None, write_s: float = 0.002):
        import asyncio
        import iledsign

        class Client:
            mtu_size = 247
            writes = 0

            async def write_gatt_char(self, _char, _data, response=False):
                Client.writes += 1
                await asyncio.sleep(write_s)

        sign = iledsign.Sign.__new__(iledsign.Sign)
        sign.client = Client()

        async def ack(op, _timeout):
            return bytes([0x54, op, 0, 0, 1])
        sign._wait = ack
        sign._drain = lambda: None

        async def go():
            t0 = time.monotonic()
            fin = None if finish_in is None else (
                [t0 + x for x in finish_in] if isinstance(finish_in, list) else t0 + finish_in)
            ok = await sign.send_payload(bytes(payload_len), finish_at=fin)
            return ok, time.monotonic() - t0
        ok, took = asyncio.run(go())
        return ok, took, sign.last_paced_seconds, Client.writes

    def test_paced_upload_completes_on_time(self):
        ok, took, paced, writes = self.run_upload(40_000, 4.0)
        self.assertTrue(ok)
        self.assertGreater(writes, 150)
        self.assertAlmostEqual(took, 4.0 - 0.15, delta=0.35)   # the end ack takes the last moment
        self.assertGreater(paced, 0.5)

    def test_a_missed_moment_falls_back_to_the_next_one(self):
        ok, took, paced, _w = self.run_upload(40_000, [0.05, 3.0])   # 0.05 s cannot be made
        self.assertTrue(ok)
        self.assertAlmostEqual(took, 3.0 - 0.15, delta=0.35)

    def test_unpaced_and_late_uploads_do_not_wait(self):
        ok, took, paced, _w = self.run_upload(20_000, None)
        self.assertTrue(ok)
        self.assertEqual(paced, 0.0)
        ok, took2, paced, _w = self.run_upload(20_000, 0.01)   # already late: no pauses
        self.assertEqual(paced, 0.0)
        self.assertLess(took2, took + 1.0)


class ManualSendGateTests(unittest.TestCase):
    """A day upload waits for its moment, then gives way only to a manual send made during that wait."""

    class Sign:
        def __init__(self):
            self.uploads = []

        def panel_size(self):
            return PANEL_SIZE

        def power(self, _on):
            return {}

        def brightness(self, _level):
            return {}

        def upload(self, job, _fn, _nbytes, _size):
            self.uploads.append(job)
            return {}

    class Wait:
        """Stands in for the stop event: returns at once, running `during` as if it happened mid-wait."""

        def __init__(self, during=None):
            self.during = during

        def wait(self, _seconds):
            if self.during:
                self.during()
            return False

    def make(self, during=None):
        import tempfile
        from backend.ambient.service import AmbientController
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sign = self.Sign()
        ctrl = AmbientController(tmp.name, sign, run_scheduler=False)
        ctrl._refresh_data = lambda cfg, light=False, force=False: (None, [])
        ctrl._stop = self.Wait(during and (lambda: during(ctrl)))
        return ctrl, sign

    def test_resume_after_a_manual_send_uploads(self):
        ctrl, sign = self.make()
        ctrl.on_manual_send()
        ctrl.resume()
        ctrl._do_day(ctrl.settings.get())
        self.assertEqual(sign.uploads, ["ambient"])
        self.assertEqual(ctrl.sched.last_kind, "day")

    def test_resume_after_a_day_upload_uploads(self):
        ctrl, sign = self.make()
        ctrl._do_day(ctrl.settings.get())
        ctrl.resume()
        ctrl._do_day(ctrl.settings.get())
        self.assertEqual(sign.uploads, ["ambient", "ambient"])

    def test_a_manual_send_during_the_wait_wins(self):
        ctrl, sign = self.make(during=lambda c: c.on_manual_send())
        ctrl.settings.update({"pause_minutes_on_manual": 0})   # no pause: only the send itself counts
        ctrl._do_day(ctrl.settings.get())
        self.assertEqual(sign.uploads, [])
        self.assertEqual(ctrl.sched.last_kind, "manual")

    def test_pause_during_the_wait_wins(self):
        ctrl, sign = self.make(during=lambda c: c.pause(30))
        ctrl._do_day(ctrl.settings.get())
        self.assertEqual(sign.uploads, [])


PANEL_SIZE = (64, 16)


def _floor(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


if __name__ == "__main__":
    unittest.main()
