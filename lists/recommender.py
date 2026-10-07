"""
Daily recommendations: 5 movies and 5 TV shows for each person, chosen by AI from their lists (what they
watched and how they rated it, what they're watching and want to watch), their groups and their streaming
services.

TMDB comes first: titles similar to the ones they liked or want to watch, and what's popular on their
own services in their country, become candidates; the AI picks from those (it may add others it knows),
says why, and which of their groups each suits. Every pick is checked on TMDB again for its poster and
where to watch.

A day's set is made the first time someone opens the app that day, but only when their lists, groups or
services changed since the last set; otherwise the last one stays. The "New recommendations" button makes
a new set any time. Both are subject to the AI approval and daily limits (ai.blocked). Runs in a
background thread like imports.
"""
import hashlib
import json
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.db.models import F
from django.utils import timezone

from . import ai, lookup, tmdb
from .models import AIUsage, Entry, Recommendation, RecommendationSet

logger = logging.getLogger(__name__)

PER_KIND = 5
ASK_FOR = 8  # per kind, so there are spares when a pick is already on a list or can't be found
STALE_AFTER = timedelta(minutes=10)
RECENT = timedelta(days=21)  # not recommended again within this time
MAX_LISTED = 120  # titles per list in the prompt, newest first
SEEDS = 6  # titles per kind whose similar titles become candidates
SERVICES = 6  # streaming services per person whose popular titles become candidates
POOL = 40  # candidates per kind

SYSTEM_PROMPT = """You recommend movies and TV shows to someone who uses a watchlist app. You get their lists (what they watched, with their ratings from 1 to 5 stars, what they're watching and what they want to watch), their own groups (for example who they watch with), the streaming services they have, and candidates from TMDB.

Recommend {ask_for} movies and {ask_for} TV shows they will most likely enjoy, best first.
- Mostly choose from the candidates and give their tmdb_id. You may add a title you know well that fits better: then tmdb_id is null and title and year must be exact.
- Prefer what streams on their services; a great fit elsewhere is fine now and then.
- Never recommend anything on their lists, anything they're not interested in, or anything recommended recently.
- Match their taste from what they rated highly; avoid what resembles what they rated low. Include a less obvious find or two, not only the most popular titles.
- group: the name of the one of their groups the title suits best, exactly as given, or "" if none fits or they have no groups. Spread the titles over their groups when it fits. Respect who a group is for: a group with children only gets titles suitable for children.
- reason: one short sentence, at most 20 words, on why it suits them, referring to their taste. In English.

Call save_recommendations once with all of them."""

PICK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tmdb_id", "title", "year", "reason", "group"],
    "properties": {
        "tmdb_id": {"type": ["integer", "null"]},
        "title": {"type": "string"},
        "year": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
        "group": {"type": "string"},
    },
}

SAVE_TOOL = {
    "type": "function",
    "name": "save_recommendations",
    "description": "Save the recommended movies and TV shows.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["movies", "shows"],
        "properties": {"movies": {"type": "array", "items": PICK}, "shows": {"type": "array", "items": PICK}},
    },
}

KINDS = [(Entry.Kind.MOVIE, "movies"), (Entry.Kind.SHOW, "shows")]


# --- when -------------------------------------------------------------------------


def fingerprint(user):
    """Someone's lists, groups and services in short: when it changes, a new day brings new recommendations."""
    entries = sorted(map(str, user.entries.values_list("kind", "tmdb_id", "title", "year", "season", "status", "rating",
                                                       "tag_id")))
    data = [entries, sorted(user.tags.values_list("name", flat=True)),
            sorted(user.services), sorted(user.own_services), user.region]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def running(user):
    return user.recommendation_sets.filter(status=RecommendationSet.Status.RUNNING).first()


def daily(user):
    """Starts today's recommendations when they're due: none were made today, and the lists changed since
    the last ones (or there are none yet). Returns the new set, or None."""
    expire_stale()
    if ai.blocked(user) or not user.entries.exists():
        return None
    today = ai.today_start()
    sets = user.recommendation_sets
    if sets.filter(status=RecommendationSet.Status.RUNNING).exists() or sets.filter(created_at__gte=today).exists():
        return None  # one try a day by itself, even when it failed
    last = sets.filter(status=RecommendationSet.Status.DONE).first()
    print_ = fingerprint(user)
    if last and last.fingerprint == print_:
        return None
    return start_new(user, print_)


def request_new(user):
    """The "New recommendations" button: (the set being made, or None and why not)."""
    expire_stale()
    reason = ai.blocked(user)
    if reason:
        return None, reason
    if not user.entries.exists():
        return None, "Add a few movies or shows you liked first, so there's something to go on."
    return running(user) or start_new(user, fingerprint(user), requested=True), ""


def start_new(user, print_, requested=False):
    batch = RecommendationSet.objects.create(user=user, fingerprint=print_, requested=requested)
    start(batch)
    return batch


def start(batch):
    threading.Thread(target=run, args=(batch.pk,), daemon=True, name=f"recommend-{batch.pk}").start()


def expire_stale():
    RecommendationSet.objects.filter(
        status=RecommendationSet.Status.RUNNING, created_at__lt=timezone.now() - STALE_AFTER,
    ).update(status=RecommendationSet.Status.FAILED, message="This took too long and was stopped.",
             finished_at=timezone.now())


# --- what -------------------------------------------------------------------------


def excluded(user):
    """What not to recommend: on a list, turned down, or recommended recently. Returns ({(kind, tmdb_id)},
    {(kind, title lower)} of the ones not on TMDB, dismissed, recent) – the last two for the prompt."""
    ids, titles = set(), set()
    for kind, tmdb_id, title in user.entries.values_list("kind", "tmdb_id", "title"):
        if tmdb_id:
            ids.add((kind, tmdb_id))
        else:
            titles.add((kind, title.lower()))
    dismissed = list(user.recommendations.filter(state=Recommendation.State.DISMISSED).order_by("-pk")[:100])
    recent = list(user.recommendations.filter(batch__created_at__gte=timezone.now() - RECENT).exclude(
        state=Recommendation.State.DISMISSED).order_by("-pk")[:100])
    for rec in dismissed + recent:
        if rec.tmdb_id:
            ids.add((rec.kind, rec.tmdb_id))
        else:
            titles.add((rec.kind, rec.title.lower()))
    return ids, titles, dismissed, recent


def candidates(user, kind, excluded_ids):
    """TMDB's candidates of one kind: similar to what they liked or want to watch, and popular on their
    services. [(result, notes)] with the most reasons first; notes like "streams on Netflix"."""
    pool = {}

    def add(result, note):
        key = (kind, result["tmdb_id"])
        if key in excluded_ids:
            return
        found = pool.setdefault(result["tmdb_id"], (result, []))
        if note not in found[1]:
            found[1].append(note)

    seeds = (user.entries.filter(kind=kind, tmdb_id__isnull=False).exclude(rating__lte=2)
             .order_by(F("rating").desc(nulls_last=True), "-added_at")[:SEEDS])
    try:
        for seed in seeds:
            for result in tmdb.similar(kind, seed.tmdb_id)[:10]:
                add(result, f"similar to {seed.title}")
        if user.services:
            ids = {s["name"].lower(): s["id"] for s in tmdb.services(user.region) if s.get("id")}
            for name in user.services[:SERVICES]:
                if name.lower() in ids:
                    for result in tmdb.popular_on(kind, user.region, ids[name.lower()])[:10]:
                        add(result, f"streams on {name}")
    except tmdb.TMDBError as exc:
        logger.info("Fewer candidates for %s: %s", user, exc)
    ranked = sorted(pool.values(), key=lambda item: (-len(item[1]), -item[0]["popularity"]))
    return ranked[:POOL]


def _entry_line(entry):
    line = f"- {entry.title}" + (f" ({entry.year})" if entry.year else "")
    if entry.season:
        line += f", season {entry.season}"
    if entry.rating:
        line += f", {entry.rating}★"
    if entry.tag:
        line += f" [{entry.tag.name}]"
    return line


def prompt(user, pools, dismissed, recent):
    """What the AI gets to go on."""
    tags = [t.name for t in user.tags.all()]
    parts = [
        f"Country: {user.region}.",
        "Streaming services they have: " + (", ".join(user.services) or "none given") + ".",
    ]
    if user.own_services:
        parts.append("They also watch on: " + ", ".join(user.own_services) + ".")
    parts.append("Their groups: " + (", ".join(tags) if tags else "none") + ".")
    for kind, label in KINDS:
        entries = user.entries.filter(kind=kind).select_related("tag").order_by("-added_at")
        for status, title in [(Entry.Status.WATCHED, "watched"), (Entry.Status.WATCHING, "are watching"),
                              (Entry.Status.WANT, "want to watch")]:
            listed = [_entry_line(e) for e in entries.filter(status=status)[:MAX_LISTED]]
            if listed:
                parts.append(f"\n{label.capitalize()} they {title}:\n" + "\n".join(listed))
    if dismissed:
        parts.append("\nNot interested (never recommend):\n" + "\n".join(f"- {r}" for r in dismissed))
    if recent:
        parts.append("\nRecommended recently (don't repeat):\n" + "\n".join(f"- {r}" for r in recent))
    for kind, label in KINDS:
        if pools[kind]:
            lines = [f"- tmdb_id {r['tmdb_id']}: {r['title']}" + (f" ({r['year']})" if r["year"] else "")
                     + f" – {'; '.join(notes)}" for r, notes in pools[kind]]
            parts.append(f"\nCandidate {label} from TMDB:\n" + "\n".join(lines))
    return "\n".join(parts)


def ask_ai(user, text):
    """The AI's picks: {"movies": [...], "shows": [...]}."""
    response = ai.get_client().responses.create(
        model=settings.AI_MODEL,
        instructions=SYSTEM_PROMPT.format(ask_for=ASK_FOR),
        input=[{"role": "user", "content": text}],
        tools=[SAVE_TOOL],
        tool_choice={"type": "function", "name": "save_recommendations"},
        reasoning={"effort": settings.AI_EFFORT},
        max_output_tokens=16000,
    )
    ai.record(user, AIUsage.Kind.RECOMMEND, response)
    call = next((i for i in response.output if i.type == "function_call" and i.name == "save_recommendations"), None)
    if call is None:
        raise ai.AIError("The AI didn't answer.")
    try:
        data = json.loads(call.arguments)
        return {"movies": list(data["movies"]), "shows": list(data["shows"])}
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ai.AIError("The AI's answer couldn't be read.")


def resolve(user, kind, picks, pool, excluded_ids, excluded_titles):
    """The picks of one kind as found on TMDB, without what's excluded or repeated: up to PER_KIND, the ones
    streaming on their services first (in the AI's order), then the others."""
    by_id = {r["tmdb_id"]: r for r, _ in pool}
    tags = {t.name.lower(): t for t in user.tags.all()}
    chosen = []
    for pick in picks[:ASK_FOR]:
        title = " ".join(str(pick.get("title", "")).split())[:200]
        year = pick.get("year") if isinstance(pick.get("year"), int) else None
        found = by_id.get(pick.get("tmdb_id"))
        if found is None and title and tmdb.enabled():
            try:
                found, _ = lookup.best_match(title, year, kind, tmdb.search(title, kind))
            except tmdb.TMDBError:
                found = None
        item = {"kind": kind, "tmdb_id": None, "title": title, "year": year, "poster_path": "", "overview": "",
                "providers": []}
        if found:
            item.update(tmdb_id=found["tmdb_id"], title=found["title"][:200], year=found["year"],
                        poster_path=found["poster"], overview=found["overview"])
        elif tmdb.enabled() or not title:
            continue  # TMDB doesn't know it: likely a mistake
        key = item["tmdb_id"] or item["title"].lower()
        if ((kind, item["tmdb_id"]) in excluded_ids or (kind, item["title"].lower()) in excluded_titles
                or any((c["tmdb_id"] or c["title"].lower()) == key for c in chosen)):
            continue
        if item["tmdb_id"]:
            try:
                item["providers"] = tmdb.details(kind, item["tmdb_id"], user.region)["providers"]
            except tmdb.TMDBError:
                pass
        item["reason"] = " ".join(str(pick.get("reason", "")).split())[:300]
        item["tag"] = tags.get(str(pick.get("group", "")).strip().lower())
        chosen.append(item)
    mine = {s.lower() for s in user.all_services}
    streaming = [c for c in chosen if any(p["name"].lower() in mine for p in c["providers"])]
    return (streaming + [c for c in chosen if c not in streaming])[:PER_KIND]


def run(batch_id):
    """Makes a RecommendationSet. Safe to run in a thread."""
    try:
        batch = RecommendationSet.objects.select_related("user").get(pk=batch_id)
        user = batch.user
        try:
            excluded_ids, excluded_titles, dismissed, recent = excluded(user)
            pools = {kind: candidates(user, kind, excluded_ids) if tmdb.enabled() else [] for kind, _ in KINDS}
            picks = ask_ai(user, prompt(user, pools, dismissed, recent))
            position = 0
            for kind, label in KINDS:
                for item in resolve(user, kind, picks[label], pools[kind], excluded_ids, excluded_titles):
                    Recommendation.objects.create(batch=batch, user=user, position=position, **item)
                    position += 1
            batch.status = RecommendationSet.Status.DONE
            if not position:
                batch.message = "Nothing new was found this time."
        except (ai.AIError, tmdb.TMDBError) as exc:
            batch.status, batch.message = RecommendationSet.Status.FAILED, str(exc)
        except Exception as exc:
            logger.exception("Recommendations %s failed", batch_id)
            batch.status = RecommendationSet.Status.FAILED
            batch.message = f"Something went wrong while choosing ({exc.__class__.__name__})."
        batch.finished_at = timezone.now()
        batch.save()
    finally:
        close_old_connections()


# --- showing ----------------------------------------------------------------------


def for_home(user):
    """What the home page shows: the newest finished set's open recommendations, or why there are none."""
    expire_stale()
    shown = user.recommendation_sets.filter(status=RecommendationSet.Status.DONE).first()
    items = list(shown.items.filter(state=Recommendation.State.OPEN).select_related("tag")) if shown else []
    latest = user.recommendation_sets.first()
    movies = [r for r in items if r.kind == Entry.Kind.MOVIE]
    shows = [r for r in items if r.kind == Entry.Kind.SHOW]
    return {
        "rec_set": shown,
        "rec_running": running(user),
        "rec_failed": latest if latest and latest.status == RecommendationSet.Status.FAILED else None,
        "rec_movies": movies,
        "rec_shows": shows,
        "rec_lists": [("🎬 Movies", movies), ("📺 TV shows", shows)],
        "rec_blocked": ai.blocked(user),
    }
