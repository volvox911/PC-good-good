"""Validate Claude's identification against real PriceCharting search candidates.

AI confidence is never treated as proof. A product is auto-accepted only when:

1. the item is a video game with a readable title and a known platform;
2. the candidate's console family *and* region match the recognized platform/region;
3. the title similarity (with edition adjustments) is >= ``accept_score``;
4. it leads the runner-up on the same platform by >= ``accept_margin``; and
5. recognition confidence is >= ``min_confidence`` and the title is not obscured.

Anything else becomes ``ambiguous``/``no_match`` and is sent to the operator for a
one-click confirmation — no arbitrary pricing is ever displayed.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from rapidfuzz import fuzz

from ..config import RecognitionSettings
from ..db import Database
from ..models import Candidate
from ..pricing.service import PricingService
from ..vision.platforms import canonical_base, console_name_for, family
from ..vision.schema import CONFIDENCE_RANK, GameIdentification

log = logging.getLogger(__name__)

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_EDITION_PHRASES = [
    "players choice",
    "greatest hits",
    "platinum hits",
    "platinum",
    "nintendo selects",
    "collectors edition",
    "limited edition",
    "not for resale",
    "classics",
    "game of the year edition",
    "game of the year",
    "best of",
    "the best",
]
_STANDARD_EDITION = {"standard", "regular", "original", "normal", "none", "black label", "first print", "retail"}


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"['’`´]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    toks = [_ROMAN.get(t, t) for t in s.split()]
    while toks and toks[0] == "the":
        toks = toks[1:]
    out = " ".join(toks)
    out = re.sub(r"\blegend of zelda\b", "zelda", out)
    return out


def split_edition(product_name: str) -> tuple[str, str | None]:
    """``"Zelda Ocarina of Time [Player's Choice]"`` -> (``"Zelda Ocarina of Time"``, ``"Player's Choice"``)."""
    eds = re.findall(r"\[([^\]]+)\]", product_name)
    base = re.sub(r"\s*\[[^\]]*\]", "", product_name).strip()
    return base, (" ".join(eds) if eds else None)


def normalize_edition(edition: str | None) -> str | None:
    if not edition:
        return None
    e = norm_text(edition)
    if not e or e in _STANDARD_EDITION:
        return None
    return e


def extract_edition_from_title(title: str) -> tuple[str, str | None]:
    n = norm_text(title)
    for phrase in _EDITION_PHRASES:
        if re.search(rf"\b{phrase}\b", n):
            stripped = " ".join(re.sub(rf"\b{phrase}\b", " ", n).split())
            if stripped:
                return stripped, phrase
    return n, None


def _digit_tokens(s: str) -> set[str]:
    return {t for t in s.split() if any(c.isdigit() for c in t)}


def title_score(recognized: str, candidate_base: str) -> float:
    a, b = norm_text(recognized), norm_text(candidate_base)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    score = 0.55 * fuzz.token_sort_ratio(a, b) + 0.45 * fuzz.token_set_ratio(a, b)
    if _digit_tokens(a) != _digit_tokens(b):
        score -= 20.0  # "Mortal Kombat" vs "Mortal Kombat II", "Ocarina of Time" vs "... 3D"
    return max(0.0, min(100.0, score))


def edition_adjustment(recognized_edition: str | None, candidate_edition: str | None) -> float:
    rec = normalize_edition(recognized_edition)
    cand = normalize_edition(candidate_edition)
    if rec and cand:
        return 8.0 if fuzz.token_set_ratio(rec, cand) >= 80 else -15.0
    if rec and not cand:
        return -10.0
    if cand and not rec:
        return -14.0
    return 0.0


def valid_upc(digits: str | None) -> bool:
    if not digits or not digits.isdigit() or len(digits) not in (12, 13):
        return False
    nums = [int(c) for c in digits]
    if len(digits) == 12:
        total = 3 * sum(nums[0:11:2]) + sum(nums[1:11:2])
    else:
        total = sum(nums[0:12:2]) + 3 * sum(nums[1:12:2])
    return (10 - total % 10) % 10 == nums[-1]


@dataclass
class ScoredCandidate:
    candidate: Candidate
    score: float
    platform_ok: bool
    region_ok: bool
    edition: str | None

    @property
    def eligible(self) -> bool:
        return self.platform_ok and self.region_ok

    def to_dict(self) -> dict:
        return {
            **self.candidate.to_dict(),
            "score": round(self.score, 1),
            "platformMatch": self.platform_ok,
            "regionMatch": self.region_ok,
            "edition": self.edition,
        }


@dataclass
class MatchResult:
    status: str  # matched | ambiguous | no_match | not_a_game | unreadable
    product: Candidate | None = None
    score: float | None = None
    via: str | None = None  # match_cache | upc | search | manual | visual_memory
    reason: str = ""
    key: str | None = None
    suggestions: list[ScoredCandidate] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "product": self.product.to_dict() if self.product else None,
            "score": None if self.score is None else round(self.score, 1),
            "via": self.via,
            "reason": self.reason,
            "suggestions": [s.to_dict() for s in self.suggestions[:10]],
        }


def match_key(rec: GameIdentification, default_region: str) -> str | None:
    if not rec.title:
        return None
    base = canonical_base(rec.platform)
    if not base:
        return None
    title, ed_from_title = extract_edition_from_title(rec.title)
    edition = normalize_edition(rec.edition) or ed_from_title or ""
    region = rec.region if rec.region != "unknown" else default_region
    return f"{title}|{base}|{region}|{edition}"


class Matcher:
    def __init__(self, pricing: PricingService, db: Database, settings: Callable[[], RecognitionSettings]) -> None:
        self.pricing = pricing
        self.db = db
        self._settings = settings

    # -- match cache ---------------------------------------------------------------------------
    def cached_match(self, key: str) -> tuple[Candidate, str] | None:
        row = self.db.query_one("SELECT * FROM match_cache WHERE key=?", (key,))
        if row is None:
            return None
        self.db.execute(
            "UPDATE match_cache SET hits=hits+1, last_used_at=? WHERE key=?", (time.time(), key)
        )
        return Candidate(row["product_id"], row["product_name"], row["console_name"]), row["source"]

    def remember(self, key: str, product: Candidate, source: str, score: float | None = None) -> None:
        now = time.time()
        existing = self.db.query_one("SELECT source FROM match_cache WHERE key=?", (key,))
        if existing is not None and existing["source"] == "manual" and source == "auto":
            return  # never let an automatic match overwrite an operator correction
        self.db.execute(
            "INSERT INTO match_cache(key, product_id, product_name, console_name, source, score, created_at, "
            "last_used_at, hits) VALUES (?,?,?,?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET "
            "product_id=excluded.product_id, product_name=excluded.product_name, console_name=excluded.console_name, "
            "source=excluded.source, score=excluded.score, last_used_at=excluded.last_used_at",
            (key, product.product_id, product.product_name, product.console_name, source, score, now, now),
        )

    def forget(self, key: str) -> None:
        self.db.execute("DELETE FROM match_cache WHERE key=?", (key,))

    # -- scoring ---------------------------------------------------------------------------------
    def score_candidates(self, rec: GameIdentification, candidates: list[Candidate]) -> list[ScoredCandidate]:
        cfg = self._settings()
        rec_base = canonical_base(rec.platform)
        rec_region = rec.region if rec.region != "unknown" else cfg.default_region
        title, ed_from_title = extract_edition_from_title(rec.title or "")
        rec_edition = rec.edition or ed_from_title
        titles = [title] + [norm_text(t) for t in rec.alternate_titles]
        out = []
        for c in candidates:
            base_name, cand_ed = split_edition(c.product_name)
            ts = max(title_score(t, base_name) for t in titles if t) if any(titles) else 0.0
            # Alternate titles are hints, slightly discounted vs the printed title.
            primary = title_score(title, base_name)
            ts = max(primary, ts - 3.0)
            score = max(0.0, min(100.0, ts + edition_adjustment(rec_edition, cand_ed)))
            region, base = family(c.console_name)
            out.append(
                ScoredCandidate(
                    candidate=c,
                    score=score,
                    platform_ok=rec_base is not None and base == rec_base,
                    region_ok=region == rec_region,
                    edition=cand_ed,
                )
            )
        out.sort(key=lambda s: (not s.eligible, -s.score))
        return out

    def decide(self, rec: GameIdentification, scored: list[ScoredCandidate]) -> MatchResult:
        cfg = self._settings()
        eligible = [s for s in scored if s.eligible]
        conf_ok = CONFIDENCE_RANK[rec.confidence] >= CONFIDENCE_RANK[cfg.min_confidence]
        if rec.title_obscured and rec.confidence != "high":
            conf_ok = False
        if not eligible:
            return MatchResult(
                "no_match",
                reason="no PriceCharting product on the recognized console/region",
                suggestions=scored[:10],
            )
        best = eligible[0]
        second = eligible[1] if len(eligible) > 1 else None
        margin = best.score - second.score if second else 100.0
        if best.score < cfg.accept_score:
            return MatchResult("ambiguous", reason=f"best title match is weak ({best.score:.0f})", suggestions=scored[:10])
        if margin < cfg.accept_margin:
            return MatchResult(
                "ambiguous",
                reason=f"several close matches ({best.candidate.product_name} vs {second.candidate.product_name})",
                suggestions=scored[:10],
            )
        if not conf_ok:
            return MatchResult(
                "ambiguous",
                reason=f"recognition confidence {rec.confidence}{' (title obscured)' if rec.title_obscured else ''}",
                suggestions=scored[:10],
            )
        return MatchResult("matched", product=best.candidate, score=best.score, via="search", suggestions=scored[:10])

    # -- resolution ------------------------------------------------------------------------------
    async def resolve(self, rec: GameIdentification) -> MatchResult:
        cfg = self._settings()
        if not rec.item_present or rec.item_type == "none":
            return MatchResult("not_a_game", reason="nothing is being presented")
        if rec.item_type != "video_game":
            return MatchResult("not_a_game", reason=f"item is {rec.item_type.replace('_', ' ')}, not a game")
        if not rec.title:
            return MatchResult("unreadable", reason="title not readable")
        base = canonical_base(rec.platform)
        key = match_key(rec, cfg.default_region)
        conf_ok = CONFIDENCE_RANK[rec.confidence] >= CONFIDENCE_RANK[cfg.min_confidence] and not (
            rec.title_obscured and rec.confidence != "high"
        )

        if key:
            cached = self.cached_match(key)
            if cached and (cached[1] == "manual" or conf_ok):
                product, source = cached
                return MatchResult("matched", product=product, score=100.0, via="match_cache",
                                   reason=f"remembered ({source})", key=key)

        if valid_upc(rec.upc_digits) and conf_ok:
            try:
                p = await self.pricing.lookup_upc(rec.upc_digits)
            except ValueError:
                p = None
            if p is not None:
                region, fam_base = family(p.console_name)
                rec_region = rec.region if rec.region != "unknown" else cfg.default_region
                if base is None or (fam_base == base and region == rec_region):
                    cand = Candidate(p.product_id, p.product_name, p.console_name)
                    if key:
                        self.remember(key, cand, "auto", 100.0)
                    return MatchResult("matched", product=cand, score=100.0, via="upc", key=key)

        if base is None:
            # Without a platform we can still offer suggestions, never an automatic match.
            cands = await self.pricing.search(rec.title)
            scored = self.score_candidates(rec, cands)
            return MatchResult("ambiguous", reason="console not identified", suggestions=scored[:10],
                               queries=[rec.title], key=key)

        region = rec.region if rec.region != "unknown" else cfg.default_region
        console_q = console_name_for(base, region) or base
        title_clean, _ = extract_edition_from_title(rec.title)
        queries: list[str] = []
        for q in [f"{rec.title} {console_q}", rec.title, *(f"{t} {console_q}" for t in rec.alternate_titles[:1])]:
            q = " ".join(q.split())
            if q.lower() not in (x.lower() for x in queries):
                queries.append(q)

        merged: dict[str, Candidate] = {}
        result = MatchResult("no_match", reason="no candidates found")
        for q in queries[:3]:
            for c in await self.pricing.search(q):
                merged.setdefault(c.product_id, c)
            scored = self.score_candidates(rec, list(merged.values()))
            result = self.decide(rec, scored)
            if result.status == "matched":
                break
            # Stop early if the eligible set is already clearly ambiguous (avoid extra calls).
            if result.status == "ambiguous" and any(s.eligible and s.score >= cfg.accept_score for s in scored):
                break
        result.key = key
        result.queries = queries
        if result.status == "matched" and key and result.product:
            self.remember(key, result.product, "auto", result.score)
        log.info(
            "match %s: %r on %s -> %s (%s)",
            result.status,
            title_clean,
            console_q,
            result.product.product_name if result.product else "-",
            result.reason,
        )
        return result
