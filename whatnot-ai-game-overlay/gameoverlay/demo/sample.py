"""Sample record for the dashboard's design preview ONLY.

These are the illustrative values from the project brief. They are returned only by
the authenticated preview endpoint to the dashboard's preview frame, which renders
them under a large DEMO watermark. They are never given to the overlay hub, so they
can never appear in the OBS Browser Source.
"""

SAMPLE_PREVIEW_RECORD = {
    "title": "The Legend of Zelda: Ocarina of Time",
    "platform": "NINTENDO 64",
    "productId": "000000",
    "verified": True,
    "looseUsd": 42.50,
    "completeUsd": 109.99,
    "newUsd": 599.00,
    "looseCents": 4250,
    "completeCents": 10999,
    "newCents": 59900,
    "retrievedAt": None,
    "freshness": "fresh",
    "demo": True,
}

PREVIEW_VARIANTS = {
    "sample": SAMPLE_PREVIEW_RECORD,
    "long-title": {**SAMPLE_PREVIEW_RECORD, "title": "Teenage Mutant Ninja Turtles II: The Arcade Game — Collector's Limited Edition", "platform": "NINTENDO NES"},
    "six-figure": {**SAMPLE_PREVIEW_RECORD, "title": "Stadium Events", "platform": "NINTENDO NES", "looseUsd": 980.00, "completeUsd": 12500.00, "newUsd": 123456.78, "looseCents": 98000, "completeCents": 1250000, "newCents": 12345678},
    "low-values": {**SAMPLE_PREVIEW_RECORD, "title": "Madden NFL 2004", "platform": "PLAYSTATION 2", "looseUsd": 1.99, "completeUsd": 3.75, "newUsd": 14.99, "looseCents": 199, "completeCents": 375, "newCents": 1499},
    "missing": {**SAMPLE_PREVIEW_RECORD, "title": "EarthBound", "platform": "SUPER NINTENDO", "looseUsd": 172.44, "completeUsd": None, "newUsd": None, "looseCents": 17244, "completeCents": None, "newCents": None},
}
