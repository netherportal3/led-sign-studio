"""Ambient mode: weather, clock and world news rotating on the sign during the day.

Modules:
    settings   defaults and load/save of app\\data\\ambient\\settings.json
    weather    Open-Meteo forecast with a disk cache
    news       RSS headlines, hard-news filter, cross-outlet clustering and ranking
    glyphs     purpose-made bitmap glyphs: tall clock digits, weather icons, color scales
    screens    the 64x16 screens (clock, now, today, week, news, night) as frame lists
    rotation   one upload GIF laid out on real minute boundaries
    scheduler  pure day/night/pause decisions
    preview    CLI: fetch live data, write the rotation GIF and LED-style PNG sheets
"""
