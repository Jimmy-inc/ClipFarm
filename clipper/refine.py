"""Tighten a rough window (from Gemini, ~1 fps accurate) using local word timestamps.

Rules: never start or end mid-word; trim dead air at the edges; keep within min/max length.
Times here are relative to the downloaded section (t=0 = section start).
"""


def refine(words, start, end, sec_len, min_s, max_s, lead=0.15, tail=0.35, max_dead_air=1.5):
    start, end = max(0.0, start), min(sec_len, end)
    inside = [w for w in words if w["e"] > start and w["s"] < end]

    # Start: if a word straddles the cut, back up to just before it.
    for w in inside:
        if w["s"] < start < w["e"]:
            start = max(0.0, w["s"] - lead)
        break
    # End: if a word straddles the cut, extend to just after it.
    for w in reversed(inside):
        if w["s"] < end < w["e"]:
            end = min(sec_len, w["e"] + tail)
        break

    # Don't open or close mid-sentence: move out to the phrase boundary (a pause >= phrase_gap)
    # if one is within max_shift seconds.
    phrase_gap, max_shift = 0.35, 2.5
    before = [w for w in words if w["e"] <= start + 0.01]
    if before and start - before[-1]["e"] < phrase_gap:
        i = len(before) - 1
        while i > 0 and before[i]["s"] - before[i - 1]["e"] < phrase_gap:
            i -= 1
        if start - before[i]["s"] <= max_shift:
            start = max(0.0, before[i]["s"] - lead)
    after = [w for w in words if w["s"] >= end - 0.01]
    if after and after[0]["s"] - end < phrase_gap:
        i = 0
        while i + 1 < len(after) and after[i + 1]["s"] - after[i]["e"] < phrase_gap:
            i += 1
        if after[i]["e"] - end <= max_shift:
            end = min(sec_len, after[i]["e"] + tail)

    # Trim long silences at the edges only when there is speech to anchor to.
    # (Gameplay with no talking keeps Gemini's visual cut untouched.)
    inside = [w for w in words if w["e"] > start and w["s"] < end]
    if inside:
        if inside[0]["s"] - start > max_dead_air:
            start = inside[0]["s"] - lead - 0.5
        if end - inside[-1]["e"] > max_dead_air + 1.5:   # allow a beat after the last word
            end = inside[-1]["e"] + tail + 1.0

    # Length limits.
    if end - start > max_s:
        end = start + max_s
        cut = [w for w in words if w["s"] < end < w["e"]]
        if cut:                                    # don't land mid-word after truncating
            end = cut[0]["s"] - 0.05
    if end - start < min_s:
        need = min_s - (end - start)
        end = min(sec_len, end + need)
        start = max(0.0, end - max(min_s, end - start))
    return round(start, 2), round(end, 2)
