"""
Finding titles on TMDB and keeping entries' details (poster, genres, where to watch) up to date.
"""
import logging
import re
import threading
import unicodedata
from datetime import timedelta

from django.core.cache import cache
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from . import tmdb
from .models import Entry

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(days=7)
REFRESH_EVERY = 6 * 60 * 60


def norm(title):
    """A title reduced to what matters for comparing: "The Lord of the Rings: Return" ≈ "lord of the rings return"."""
    title = unicodedata.normalize("NFKD", title.casefold().replace("&", " and "))
    title = "".join(c for c in title if not unicodedata.combining(c))
    title = " ".join(re.sub(r"[^\w]+", " ", title).split())
    return title.removeprefix("the ").removeprefix("a ")


def best_match(title, year, kind, results):
    """The result that is most likely meant, and whether that's certain enough to not ask: (result, sure).
    kind is a hint ("movie", "show") or None; results come in TMDB's order (most relevant first)."""
    if not results:
        return None, False
    wanted = norm(title)

    def exact(r):
        return wanted in (norm(r["title"]), norm(r["original_title"]))

    def year_ok(r):
        return year is None or (r["year"] is not None and abs(r["year"] - year) <= 1)

    def kind_ok(r):
        return kind is None or r["kind"] == kind

    ranked = sorted(
        enumerate(results),
        key=lambda ir: (-(exact(ir[1]) and year_ok(ir[1]) and kind_ok(ir[1])), -exact(ir[1]), -kind_ok(ir[1]), ir[0]),
    )
    best = ranked[0][1]
    sure = exact(best) and year_ok(best) and kind_ok(best)
    if sure and year is None:
        # Without a year, a remake or a show of the same name could be meant; sure only if this one stands out.
        rivals = [r for r in results if r is not best and exact(r) and kind_ok(r)]
        sure = all(r["popularity"] * 3 <= best["popularity"] for r in rivals)
    return best, sure


def candidate(result):
    """A search result as kept on a PendingItem."""
    return {k: result[k] for k in ("tmdb_id", "kind", "title", "year", "poster", "overview")}


def fill(entry):
    """Fills in TMDB's details for an entry with a tmdb_id. False if TMDB couldn't be reached."""
    try:
        entry.apply_details(tmdb.details(entry.kind, entry.tmdb_id, entry.user.region))
    except tmdb.TMDBError:
        return False
    return True


def refresh_linked(user_id):
    """Fetches TMDB's details and where to watch again for someone's titles that are on TMDB. Returns how many."""
    updated = 0
    for entry in Entry.objects.filter(user_id=user_id, tmdb_id__isnull=False).select_related("user"):
        if fill(entry):
            entry.save()
            updated += 1
    return updated


def refresh(user_id):
    """Updates where to watch for all of someone's titles, e.g. after changing country. Safe to run in a
    thread. (Titles typed in by hand are matched by an update in the Manage tab, see importer.)"""
    try:
        refresh_linked(user_id)
    except Exception:
        logger.exception("Refreshing the titles of user %s failed", user_id)
    finally:
        close_old_connections()


def start_refresh(user):
    threading.Thread(target=refresh, args=(user.pk,), daemon=True, name=f"refresh-{user.pk}").start()


def refresh_if_stale(user):
    """Streaming catalogues change: refreshes someone's titles in the background when some weren't
    checked for a week. At most every few hours per person, so opening lists stays fast."""
    if not tmdb.enabled() or not cache.add(f"refresh:{user.pk}", True, REFRESH_EVERY):
        return
    stale = Entry.objects.filter(user=user, tmdb_id__isnull=False, providers_sync=True).filter(
        Q(providers_checked_at__isnull=True) | Q(providers_checked_at__lt=timezone.now() - STALE_AFTER)
    )
    if stale.exists():
        start_refresh(user)
