"""
What your friends have been up to: the titles they added or watched since your last visit.

A visit is opening the app after at least VISIT_GAP away; what's new is counted from the visit before,
so it stays visible while you look around (see visit()).
"""
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from lists.models import Entry

from .models import Friendship

VISIT_GAP = timedelta(hours=1)
FIRST_VISIT_LOOKBACK = timedelta(days=14)


def visit(user):
    """Notes that someone opened the app. Returns since when things count as new for them."""
    now = timezone.now()
    if user.last_visit_at is None or now - user.last_visit_at > VISIT_GAP:
        user.news_since = user.last_visit_at
    user.last_visit_at = now
    user.save(update_fields=["last_visit_at", "news_since"])
    return since(user)


def since(user):
    return user.news_since or (user.date_joined if user.date_joined > timezone.now() - FIRST_VISIT_LOOKBACK
                               else timezone.now() - FIRST_VISIT_LOOKBACK)


def new_filter(since, prefix=""):
    """Entries added or watched since then."""
    return Q(**{f"{prefix}added_at__gt": since}) | Q(**{f"{prefix}watched_at__gt": since})


def friends_of(user):
    return Friendship.objects.filter(user=user, friend__is_active=True).values("friend")


def mark_mine(user, entries):
    """Sets on_my_list on someone else's entries: the same title (and season) is on your list."""
    mine, my_ids = set(), set()
    for kind, title, year, tmdb_id, season in user.entries.values_list("kind", "title", "year", "tmdb_id", "season"):
        mine.add((kind, title.lower(), year, season))
        if tmdb_id:
            my_ids.add((kind, tmdb_id, season))
    for entry in entries:
        entry.on_my_list = ((entry.kind, entry.tmdb_id, entry.season) in my_ids
                            or (entry.kind, entry.title.lower(), entry.year, entry.season) in mine)
    return entries


def news(user, since, per_friend=12):
    """Per friend, newest first: [{"friend", "items": [(entry, "added"|"watched", when)], "added", "watched"}]."""
    entries = list(
        Entry.objects.filter(user__in=friends_of(user)).filter(new_filter(since))
        .select_related("user").order_by("-added_at")
    )
    mark_mine(user, entries)
    by_friend = {}
    for entry in entries:
        watched = entry.watched_at and entry.watched_at > since and entry.status == Entry.Status.WATCHED
        what, when = ("watched", entry.watched_at) if watched else ("added", entry.added_at)
        group = by_friend.setdefault(entry.user_id, {"friend": entry.user, "items": [], "added": 0, "watched": 0})
        group["items"].append((entry, what, when))
        group[what] += 1
    groups = sorted(by_friend.values(), key=lambda g: max(when for _, _, when in g["items"]), reverse=True)
    for group in groups:
        group["items"].sort(key=lambda item: item[2], reverse=True)
        group["more"] = max(0, len(group["items"]) - per_friend)
        group["items"] = group["items"][:per_friend]
    return groups


def with_activity(people, since):
    """Annotates friends (User queryset) with their counts, what's new since then and when they were last active."""
    return people.annotate(
        movies=Count("entries", filter=Q(entries__kind=Entry.Kind.MOVIE), distinct=True),
        shows=Count("entries", filter=Q(entries__kind=Entry.Kind.SHOW), distinct=True),
        new=Count("entries", filter=new_filter(since, "entries__"), distinct=True),
        last_added=Max("entries__added_at"),
        last_watched=Max("entries__watched_at"),
    )
