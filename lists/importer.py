"""
Imports a pasted list of movies and TV shows. Each title becomes a PendingItem that its owner
confirms (or discards) before it goes on a list.

The text is read line by line, and each line is looked up on TMDB. AI only steps in where that
isn't enough, and only if the person may use it (see ai.blocked): for text that isn't a list
(e.g. a message with recommendations), and for lines TMDB has no clear match for (typos, titles in
another language, "that new Nolan film"). Runs in a background thread like Chef's AI jobs.
"""
import json
import logging
import re
import threading
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import ai, lookup, tmdb
from .models import AIUsage, Entry, Import, PendingItem

logger = logging.getLogger(__name__)

MAX_TITLES = 200
STALE_AFTER = timedelta(minutes=10)

# "- ", "* ", "• ", "1. ", "2) ", "[ ] ", "[x] " at the start of a line.
BULLET = re.compile(r"^\s*(?:(?:[-*•·◦▪–—>]+|\[[ xX]?\]|\(?\d{1,3}[.):\]]|#\d+)\s*)+")
# A year at the end: "(2019)", "[2019]", "(2019-2023)", "(2019–)", "- 2019", ", 2019", " 2019".
YEAR = re.compile(r"\s*(?:[(\[]\s*((?:18|19|20)\d\d)(?:\s*[-–]\s*(?:(?:19|20)\d\d)?)?\s*[)\]]|[-–,]?\s+((?:18|19|20)\d\d))\s*$")
# Words that say what it is: "(TV)", "[series]", "(movie)".
KIND_TAG = re.compile(r"\s*[(\[]\s*(tv|tv show|tv series|series|show|miniseries|mini-series|movie|film)\s*[)\]]", re.I)
# "Season 2", "S02", "S1E4", "Staffel 3" at the end: a show.
SEASON = re.compile(r"[\s,:–-]+(?:season|staffel|temporada|saison)\s*(\d{1,3})\s*$|[\s,:–-]+s(\d{1,2})(?:\s*e\d{1,3})?\s*$", re.I)
# Section headings that say what the lines below them are: "Movies:", "TV shows", "Series:".
HEADING = re.compile(r"^\s*(?:#+\s*)?(movies?|films?|tv|tv shows?|tv series|shows?|series)\s*:?\s*$", re.I)

SYSTEM_PROMPT = """You help someone add movies and TV shows to their watchlist app. They pasted some text: a list, notes or a message with recommendations.

Find every movie and TV show the text mentions, and call save_titles once with all of them.
- title: the official title as listed on TMDB or IMDb, preferably in English. Fix typos and abbreviations. If you can't tell which title is meant, give the text as written and set sure to false.
- year: the year of release (for shows, the year the first season started), or null if you don't know it.
- kind: "movie" or "show" (TV series, miniseries, documentary series).
- season: for a show, the number of the season meant if it's a specific one ("The Bear S2", "the new season of Severance" when you know which it is), else null for the whole show.
- line: the piece of the text it came from, copied exactly.
- Leave out people, books, games and anything else that isn't a movie or TV show. Each title once."""

SAVE_TITLES_TOOL = {
    "type": "function",
    "name": "save_titles",
    "description": "Save the movies and TV shows found in the text.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["titles"],
        "properties": {
            "titles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["line", "title", "year", "kind", "season", "sure"],
                    "properties": {
                        "line": {"type": "string"},
                        "title": {"type": "string"},
                        "year": {"type": ["integer", "null"]},
                        "kind": {"type": "string", "enum": ["movie", "show"]},
                        "season": {"type": ["integer", "null"]},
                        "sure": {"type": "boolean"},
                    },
                },
            },
        },
    },
}


# --- reading the text --------------------------------------------------------------


def kind_word(word):
    word = word.lower()
    return Entry.Kind.MOVIE if word.startswith(("movie", "film")) else Entry.Kind.SHOW


def parse_line(text, kind=None):
    """One line of a list: {"line", "title", "year", "kind", "whole"}, kind being a hint or None. None if
    it's empty. whole: the title including a year-like number at its end ("Blade Runner 2049"), when
    that may be part of the title rather than the year."""
    line = " ".join(text.split())[:300]
    title = BULLET.sub("", line)
    tag = KIND_TAG.search(title)
    if tag:
        kind = kind_word(tag.group(1))
        title = KIND_TAG.sub("", title)
    year, whole = None, ""
    match = YEAR.search(title)
    if match and YEAR.sub("", title).strip():
        year = int(match.group(1) or match.group(2))
        if match.group(2):
            whole = title.strip()
        title = YEAR.sub("", title)
    season = None
    match = SEASON.search(title)
    if match and SEASON.sub("", title).strip():
        kind = Entry.Kind.SHOW
        season = int(match.group(1) or match.group(2)) or None
        title = SEASON.sub("", title)
    title = title.strip(" \t-–—,;:.\"'“”‘’")
    if not title:
        return None
    return {"line": line, "title": title[:200], "year": year, "kind": kind, "season": season, "whole": whole[:200]}


def parse(text):
    """The titles in a pasted list, one per line. Headings like "Movies:" say what the lines below are."""
    lines = [line for line in text.splitlines() if line.strip()]
    # One long line with commas or semicolons is a list too: "Dune, Arrival; Severance".
    if len(lines) == 1 and re.search(r"[,;]", lines[0]):
        lines = [part for part in re.split(r"[,;]", lines[0]) if part.strip()]
    titles, kind = [], None
    for line in lines:
        heading = HEADING.match(line)
        if heading:
            kind = kind_word(heading.group(1))
            continue
        parsed = parse_line(line, kind)
        if parsed:
            titles.append(parsed)
    return titles[:MAX_TITLES]


def looks_like_prose(text):
    """A message or notes rather than a list: long lines with sentences in them."""
    lines = [line for line in text.splitlines() if line.strip()]
    return any(len(line) > 120 for line in lines) or any(re.search(r"[a-z]\. [A-Z]", line) for line in lines)


# --- AI ----------------------------------------------------------------------------


ONLY = {Entry.Kind.MOVIE: "All of these are movies.", Entry.Kind.SHOW: "All of these are TV shows."}


def ask_ai(user, text, only=None):
    """The titles AI finds in the text: [{"line", "title", "year", "kind", "sure"}, ...].
    only: "movie" or "show" when the list is said to be only those."""
    response = ai.get_client().responses.create(
        model=settings.AI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=[{"role": "user", "content": f"{ONLY[only]}\n\n{text}" if only else text}],
        tools=[SAVE_TITLES_TOOL],
        tool_choice={"type": "function", "name": "save_titles"},
        reasoning={"effort": settings.AI_EFFORT},
        max_output_tokens=16000,
    )
    ai.record(user, AIUsage.Kind.IMPORT, response)
    call = next((i for i in response.output if i.type == "function_call" and i.name == "save_titles"), None)
    if call is None:
        raise ai.AIError("The AI didn't answer.")
    try:
        titles = json.loads(call.arguments)["titles"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ai.AIError("The AI's answer couldn't be read.")
    found = []
    for t in titles[:MAX_TITLES]:
        title = " ".join(str(t.get("title", "")).split())[:200]
        if title:
            year = t.get("year")
            found.append({
                "line": " ".join(str(t.get("line") or title).split())[:300],
                "title": title,
                "year": year if isinstance(year, int) and 1870 <= year <= 2100 else None,
                "kind": only or (t.get("kind") if t.get("kind") in ("movie", "show") else None),
                "season": t["season"] if isinstance(t.get("season"), int) and 0 < t["season"] <= 500 else None,
                "sure": bool(t.get("sure")),
            })
    return found


# --- matching ----------------------------------------------------------------------


def match(parsed, only=None):
    """Looks a parsed title up on TMDB: (item fields, sure). only: "movie" or "show" when the list is
    only those, so nothing else is looked for."""
    if only:
        parsed = {**parsed, "kind": only, "season": parsed.get("season") if only == Entry.Kind.SHOW else None}
    item = {"line": parsed["line"], "title": parsed["title"], "year": parsed["year"],
            "kind": parsed["kind"] or Entry.Kind.MOVIE, "season": parsed.get("season"),
            "tmdb_id": None, "poster_path": "", "candidates": []}
    if not tmdb.enabled():
        return item, False
    results = tmdb.search(parsed["title"], parsed["kind"])
    if not results and parsed["kind"] and not only:
        results = tmdb.search(parsed["title"])  # the hint may be wrong
    best, sure = lookup.best_match(parsed["title"], parsed["year"], parsed["kind"], results)
    if not sure and parsed.get("whole"):
        # "Blade Runner 2049": the number may be part of the title.
        whole_results = tmdb.search(parsed["whole"], parsed["kind"])
        whole_best, whole_sure = lookup.best_match(parsed["whole"], None, parsed["kind"], whole_results)
        if whole_sure:
            best, sure, results = whole_best, True, whole_results
    item["candidates"] = [lookup.candidate(r) for r in results[:6]]
    if best:
        item.update(tmdb_id=best["tmdb_id"], kind=best["kind"], title=best["title"][:200],
                    year=best["year"], poster_path=best["poster"])
        if best["kind"] != Entry.Kind.SHOW:
            item["season"] = None
        if best not in results[:6]:
            item["candidates"].insert(0, lookup.candidate(best))
    return item, sure


def typed_entries(user):
    """Someone's titles typed in by hand, read like the lines of an import, each knowing its entry."""
    parsed = []
    for entry in user.entries.filter(tmdb_id__isnull=True).order_by("pk")[:MAX_TITLES]:
        # Its list says what it is; a year-like number at its end may still be part of the title.
        line = parse_line(entry.title, entry.kind) or {"line": entry.title, "title": entry.title, "whole": ""}
        line.update(line=entry.title, kind=entry.kind, year=entry.year or line.get("year"), entry=entry.pk)
        parsed.append(line)
    return parsed


def read(job):
    """The items of an import: [(fields, sure), ...], and a note about how the text was read.
    For an update, each item's fields also have the entry_id it's for."""
    note = []
    why_not_ai = ai.blocked(job.user)
    only = job.kind or None
    if job.purpose == Import.Purpose.LINK:
        parsed = typed_entries(job.user)
    elif looks_like_prose(job.text) and not why_not_ai:
        job.used_ai = True
        parsed = ask_ai(job.user, job.text, only)
    else:
        parsed = parse(job.text)
    found = [match(p, only) for p in parsed]

    # Lines TMDB has no certain match for, or that couldn't be looked up: AI may know what's meant.
    unsure = [i for i, (_, sure) in enumerate(found) if not sure]
    if unsure and not job.used_ai and not why_not_ai:
        job.used_ai = True
        answers = ask_ai(job.user, "\n".join(found[i][0]["line"] for i in unsure), only)
        by_line = {a["line"].casefold(): a for a in answers}
        for i in unsure:
            answer = by_line.get(found[i][0]["line"].casefold())
            if answer:
                item, sure = match(answer)
                found[i] = (item, sure if tmdb.enabled() else answer["sure"])
    elif unsure and why_not_ai and settings.OPENAI_API_KEY:
        note.append(f"AI could have helped with the unclear ones: {why_not_ai}")
    for p, (item, _) in zip(parsed, found):
        if p.get("entry"):
            item["entry_id"] = p["entry"]
    return found, " ".join(note)


def on_list(user, item):
    entries = user.entries.filter(kind=item["kind"], season=item["season"])
    if item["tmdb_id"]:
        return entries.filter(tmdb_id=item["tmdb_id"]).exists() or entries.filter(
            tmdb_id__isnull=True, title__iexact=item["title"], year=item["year"]).exists()
    return entries.filter(title__iexact=item["title"], year=item["year"]).exists()


def save(job, found):
    """Keeps what's new as pending items: not what's on a list or waiting already. Returns (new, skipped)."""
    waiting = {(p.kind, p.tmdb_id or p.title.lower(), p.year if not p.tmdb_id else None, p.season)
               for p in job.user.pending.all()}
    new = skipped = 0
    for item, sure in found:
        key = (item["kind"], item["tmdb_id"] or item["title"].lower(), item["year"] if not item["tmdb_id"] else None, item["season"])
        if key in waiting or on_list(job.user, item):
            skipped += 1
            continue
        waiting.add(key)
        PendingItem.objects.create(user=job.user, source=job, status=job.add_as, sure=sure, **item)
        new += 1
    return new, skipped


def save_links(job, found):
    """Links titles typed by hand to their certain TMDB match; the others wait to be checked, as pending
    items for their entry. Returns (linked, to check)."""
    linked = waiting = 0
    for item, sure in found:
        entry = job.user.entries.filter(pk=item.pop("entry_id", None), tmdb_id__isnull=True).select_related("user").first()
        if entry is None:
            continue
        # A season typed in the title ("The Bear S2") becomes the entry's season.
        item["season"] = (item["season"] or entry.season) if item["kind"] == Entry.Kind.SHOW else None
        # Two titles typed by hand can be the same one ("Indiana Jones" and "Indiana Jones 1").
        taken = bool(item["tmdb_id"]) and job.user.entries.filter(
            kind=item["kind"], tmdb_id=item["tmdb_id"], season=item["season"]).exclude(pk=entry.pk).exists()
        if sure and item["tmdb_id"] and not taken and item["kind"] == entry.kind:
            entry.tmdb_id, entry.season = item["tmdb_id"], item["season"]
            if lookup.fill(entry):
                entry.save()
                PendingItem.objects.filter(entry=entry).delete()
                linked += 1
                continue
        PendingItem.objects.filter(entry=entry).delete()  # an earlier update's suggestion
        PendingItem.objects.create(user=job.user, source=job, entry=entry, status=entry.status,
                                   sure=sure and not taken, **item)
        waiting += 1
    return linked, waiting


def update(job):
    """An update of someone's titles: where to watch again, and matches for those typed in by hand."""
    refreshed = lookup.refresh_linked(job.user_id)
    found, note = read(job)
    linked, job.found = save_links(job, found)
    parts = [f"Updated {refreshed} title{'s' if refreshed != 1 else ''} from TMDB."]
    if linked:
        parts.append(f"Found {linked} of the ones you typed in by hand.")
    if job.found:
        parts.append(f"{job.found} {'needs' if job.found == 1 else 'need'} you to pick the right title.")
    return " ".join(parts + [note]).strip()


def run(import_id):
    """Reads an Import. Safe to run in a thread."""
    try:
        job = Import.objects.select_related("user").get(pk=import_id)
        try:
            if job.purpose == Import.Purpose.LINK:
                note = update(job)
            else:
                found, note = read(job)
                job.found, skipped = save(job, found)
                if skipped:
                    note = f"{skipped} {'was' if skipped == 1 else 'were'} already on your lists or waiting. {note}"
            job.message = note.strip()
            job.status = Import.Status.DONE
        except (tmdb.TMDBError, ai.AIError) as exc:
            job.status, job.message = Import.Status.FAILED, str(exc)
        except Exception as exc:
            logger.exception("Import %s failed", import_id)
            job.status = Import.Status.FAILED
            job.message = f"Something went wrong while reading the list ({exc.__class__.__name__}). Please try again."
        job.finished_at = timezone.now()
        job.save()
    finally:
        close_old_connections()


def start(job):
    threading.Thread(target=run, args=(job.pk,), daemon=True, name=f"import-{job.pk}").start()


def expire_stale():
    Import.objects.filter(status=Import.Status.RUNNING, created_at__lt=timezone.now() - STALE_AFTER).update(
        status=Import.Status.FAILED, message="This took too long and was stopped. Please try again.",
        finished_at=timezone.now(),
    )
