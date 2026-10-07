"""
The tools an assistant can use through MCP. Each works on the lists of the person who connected the
assistant (and what their friends share with them), through the same functions as the web pages. Results
are plain JSON-like dicts; a ToolError is shown to the assistant as a failed call with its message.

Nothing here uses Popcorn's own AI (imports, new recommendations): the assistant does that thinking itself.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from friends import activity
from lists import lookup, tmdb
from lists.models import Entry, Recommendation, RecommendationSet
from lists.views import existing

TOOLS = {}
KINDS = {"movie": Entry.Kind.MOVIE, "show": Entry.Kind.SHOW}
STATUSES = Entry.Status.values  # want, watching (shows only), watched
MAX_TITLES = 300


class ToolError(Exception):
    """Something the assistant should tell the person, or correct and try again."""


@dataclass
class Context:
    user: object
    base_url: str


def tool(name, title, description, properties=None, required=(), read_only=False, destructive=False,
         idempotent=False, open_world=False):
    def register(function):
        TOOLS[name] = {
            "function": function,
            "spec": {
                "name": name,
                "title": title,
                "description": description,
                "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required),
                                "additionalProperties": False},
                "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                                "idempotentHint": idempotent, "openWorldHint": open_world},
            },
        }
        return function
    return register


# --- arguments -------------------------------------------------------------------------------

KIND = {"type": "string", "enum": ["movie", "show"], "description": "movie or show (TV show)."}
STATUS = {"type": "string", "enum": STATUSES,
          "description": "want (to watch), watching (TV shows only) or watched."}
RATING = {"type": ["integer", "null"], "minimum": 0, "maximum": 5, "description": "1 to 5 stars; 0 or null clears it."}
GROUP = {"type": "string", "maxLength": 40,
         "description": "The name of one of their groups (see get_lists), or \"\" for none."}


def text(args, name, max_length, required=False):
    value = " ".join(str(args.get(name) or "").split())
    if required and not value:
        raise ToolError(f"{name} is required.")
    if len(value) > max_length:
        raise ToolError(f"{name} can be at most {max_length} characters.")
    return value


def number(args, name, low, high, required=False):
    value = args.get(name)
    if value is None or value == "":
        if required:
            raise ToolError(f"{name} is required.")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"{name} must be a whole number from {low} to {high}.")
    return value


def choice(args, name, choices, default=None):
    value = args.get(name) or default
    if value not in choices:
        raise ToolError(f"{name} must be one of: {', '.join(choices)}.")
    return value


def group_named(context, name):
    """One of the person's groups by name (any case), or None for ""."""
    if not name:
        return None
    tags = list(context.user.tags.all())
    found = next((t for t in tags if t.name.lower() == name.lower()), None)
    if found is None:
        names = ", ".join(t.name for t in tags) or "none yet (they're made in the app)"
        raise ToolError(f"There's no group called {name!r}. Their groups: {names}.")
    return found


def entry_in(context, args):
    entry = (context.user.entries.select_related("tag").filter(pk=number(args, "entry_id", 1, 2**31, required=True))
             .first())
    if entry is None:
        raise ToolError("No such title on their lists. Use get_lists for the entry_id.")
    return entry


def entry_info(context, entry):
    return {
        "entry_id": entry.pk,
        "kind": entry.kind,
        "title": entry.title,
        "year": entry.year,
        "season": entry.season,
        "status": entry.status,
        "rating": entry.rating,
        "group": entry.tag.name if entry.tag_id else None,
        "where_to_watch": entry.provider_names,
        "genres": entry.genres,
        "notes": entry.notes,
        "tmdb_id": entry.tmdb_id,
        "added": entry.added_at.date().isoformat(),
        "watched": entry.watched_at.date().isoformat() if entry.watched_at else None,
        "url": context.base_url + reverse("lists:entry", args=[entry.pk]),
    }


# --- reading ---------------------------------------------------------------------------------


@tool("get_lists", "Get their lists",
      "Their movies and/or TV shows, newest first, optionally only one status, one group or matching a search. "
      "Also returns the names of their groups.",
      {"kind": {**KIND, "description": "movie or show; leave out for both."},
       "status": {**STATUS, "description": "Only this status; leave out for all."},
       "group": {**GROUP, "description": "Only titles in this group; \"none\" for titles in no group."},
       "search": {"type": "string", "maxLength": 100,
                  "description": "Words to look for in titles, notes, genres and where to watch."},
       "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TITLES, "description": f"Default 100, at most {MAX_TITLES}."}},
      read_only=True, idempotent=True)
def get_lists(context, args):
    entries = context.user.entries.select_related("tag").order_by("-added_at")
    if args.get("kind"):
        entries = entries.filter(kind=choice(args, "kind", KINDS))
    if args.get("status"):
        entries = entries.filter(status=choice(args, "status", STATUSES))
    group = text(args, "group", 40)
    if group.lower() == "none":
        entries = entries.filter(tag__isnull=True)
    elif group:
        entries = entries.filter(tag=group_named(context, group))
    words = lookup.norm(text(args, "search", 100)).split()
    rows = [e for e in entries if all(w in e.search_text for w in words)] if words else list(entries)
    limit = number(args, "limit", 1, MAX_TITLES) or 100
    return {
        "titles": [entry_info(context, e) for e in rows[:limit]],
        "total": len(rows),
        "truncated": len(rows) > limit,
        "groups": [t.name for t in context.user.tags.all()],
    }


@tool("search_titles", "Search for a movie or show",
      "Searches The Movie Database (TMDB) to find the tmdb_id of a movie or TV show to add, best matches first. "
      "Says for each whether it's already on their lists.",
      {"query": {"type": "string", "maxLength": 100}, "kind": {**KIND, "description": "movie or show; leave out for both."}},
      required=["query"], read_only=True, idempotent=True, open_world=True)
def search_titles(context, args):
    if not tmdb.enabled():
        raise ToolError("Searching isn't set up in Popcorn (no TMDB key). Add titles by name with add_title.")
    query = text(args, "query", 100, required=True)
    kind = choice(args, "kind", KINDS) if args.get("kind") else None
    try:
        results = tmdb.search(query, kind)[:10]
    except tmdb.TMDBError as exc:
        raise ToolError(str(exc))
    on_list = {}
    for entry in context.user.entries.filter(tmdb_id__in=[r["tmdb_id"] for r in results]):
        on_list.setdefault((entry.kind, entry.tmdb_id), []).append(
            {"entry_id": entry.pk, "season": entry.season, "status": entry.status})
    return {"results": [
        {"tmdb_id": r["tmdb_id"], "kind": r["kind"], "title": r["title"], "year": r["year"],
         "overview": r["overview"][:300], "on_their_lists": on_list.get((r["kind"], r["tmdb_id"]), [])}
        for r in results
    ]}


@tool("get_recommendations", "Get today's recommendations",
      "The movies and TV shows Popcorn recommended to them that they haven't added or turned down yet, with why. "
      "This only reads them; new ones are made by Popcorn itself.",
      read_only=True, idempotent=True)
def get_recommendations(context, args):
    batch = context.user.recommendation_sets.filter(status=RecommendationSet.Status.DONE).first()
    if batch is None:
        return {"recommendations": [], "chosen": None}
    items = batch.items.filter(state=Recommendation.State.OPEN).select_related("tag")
    return {
        "chosen": batch.created_at.date().isoformat(),
        "recommendations": [
            {"recommendation_id": r.pk, "kind": r.kind, "title": r.title, "year": r.year, "tmdb_id": r.tmdb_id,
             "reason": r.reason, "group": r.tag.name if r.tag_id else None,
             "where_to_watch": [p["name"] for p in r.providers]}
            for r in items
        ],
    }


@tool("get_friends_activity", "What friends added or watched",
      "The titles their friends added to their lists or watched recently, per friend, newest first. By default "
      "since their last visit to Popcorn.",
      {"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "The last so many days instead."}},
      read_only=True, idempotent=True)
def get_friends_activity(context, args):
    days = number(args, "days", 1, 90)
    since = timezone.now() - timedelta(days=days) if days else activity.since(context.user)
    return {
        "since": since.date().isoformat(),
        "friends": [
            {"friend": str(group["friend"]),
             "titles": [{"kind": e.kind, "title": e.title, "year": e.year, "season": e.season, "what": what,
                         "when": when.date().isoformat(), "rating": e.rating if what == "watched" else None,
                         "tmdb_id": e.tmdb_id, "on_their_lists": e.on_my_list}
                        for e, what, when in group["items"]],
             "more": group["more"]}
            for group in activity.news(context.user, since, per_friend=30)
        ],
    }


# --- changing --------------------------------------------------------------------------------


@tool("add_title", "Add a movie or show",
      "Puts a movie or TV show on their list. Give the tmdb_id from search_titles (then title and year come from "
      "TMDB, with poster, genres and where to watch), or just a title. A TV show can be added as the whole show "
      "or one season. Titles already on their list aren't added twice.",
      {"kind": KIND,
       "tmdb_id": {"type": "integer", "minimum": 1},
       "title": {"type": "string", "maxLength": 200, "description": "Needed without tmdb_id."},
       "year": {"type": "integer", "minimum": 1870, "maximum": 2100},
       "season": {"type": "integer", "minimum": 1, "maximum": 500, "description": "TV shows: one season; leave out for the whole show."},
       "status": {**STATUS, "description": "Default want."},
       "rating": RATING,
       "group": GROUP,
       "notes": {"type": "string", "maxLength": 1000}},
      required=["kind"])
def add_title(context, args):
    user = context.user
    kind = choice(args, "kind", KINDS)
    tmdb_id = number(args, "tmdb_id", 1, 2**31)
    title = text(args, "title", 200, required=not tmdb_id)
    year = number(args, "year", 1870, 2100)
    season = number(args, "season", 1, 500) if kind == Entry.Kind.SHOW else None
    status = choice(args, "status", Entry.statuses_for(kind), default=Entry.Status.WANT)
    rating = number(args, "rating", 0, 5)
    tag = group_named(context, text(args, "group", 40))

    entry = Entry(user=user, kind=kind, title=title, year=year, season=season, tmdb_id=tmdb_id,
                  notes=str(args.get("notes") or "").strip()[:1000], tag=tag)
    if tmdb_id:
        if not tmdb.enabled():
            raise ToolError("TMDB isn't set up in Popcorn: add it by title instead, without tmdb_id.")
        if not lookup.fill(entry):
            raise ToolError("TMDB couldn't be reached, or doesn't know that tmdb_id for this kind. Try again later.")
    found = existing(user, kind, entry.tmdb_id, entry.title, entry.year, season)
    if found:
        raise ToolError(f"{found} is already on their list ({found.get_status_display().lower()}, "
                        f"entry_id {found.pk}). Use update_title to change it.")
    entry.set_status(status)
    entry.set_rating(rating or None)
    entry.save()
    # Added from a recommendation: it's done.
    if entry.tmdb_id:
        user.recommendations.filter(kind=kind, tmdb_id=entry.tmdb_id, state=Recommendation.State.OPEN).update(
            state=Recommendation.State.ADDED)
    return {"added": entry_info(context, entry)}


@tool("update_title", "Change a title",
      "Changes a title's status, rating, group or notes. Only what's given changes. Rating something not "
      "watched yet marks it watched.",
      {"entry_id": {"type": "integer"}, "status": STATUS, "rating": RATING, "group": GROUP,
       "notes": {"type": "string", "maxLength": 1000}},
      required=["entry_id"], idempotent=True)
def update_title(context, args):
    entry = entry_in(context, args)
    if "status" in args:
        entry.set_status(choice(args, "status", Entry.statuses_for(entry.kind)))
    if "rating" in args:
        entry.set_rating(number(args, "rating", 0, 5) or None)
    if "group" in args:
        entry.tag = group_named(context, text(args, "group", 40))
    if "notes" in args:
        entry.notes = str(args.get("notes") or "").strip()[:1000]
    entry.save()
    return {"updated": entry_info(context, entry)}


@tool("remove_title", "Remove a title",
      "Takes a title off their lists, with its rating and notes. Ask them first.",
      {"entry_id": {"type": "integer"}}, required=["entry_id"], destructive=True, idempotent=True)
def remove_title(context, args):
    entry = entry_in(context, args)
    name = str(entry)
    entry.delete()
    return {"removed": name}


@tool("dismiss_recommendation", "Turn down a recommendation",
      "Not for them: the recommendation goes, and that title won't be recommended again.",
      {"recommendation_id": {"type": "integer"}}, required=["recommendation_id"], idempotent=True)
def dismiss_recommendation(context, args):
    rec = context.user.recommendations.filter(pk=number(args, "recommendation_id", 1, 2**31, required=True)).first()
    if rec is None:
        raise ToolError("No such recommendation. Use get_recommendations for the recommendation_id.")
    rec.state = Recommendation.State.DISMISSED
    rec.save(update_fields=["state"])
    return {"dismissed": str(rec)}


def call(name, context, args):
    return TOOLS[name]["function"](context, args)
