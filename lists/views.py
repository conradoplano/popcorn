"""Your own lists of movies and TV shows: adding, rating, marking watched, editing."""
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from friends import activity
from friends.models import Friendship, can_see, incoming_requests

from . import lookup, tmdb
from .forms import AddEntryForm, EntryForm
from .models import Entry, Tag, poster_url

KIND_PAGES = {Entry.Kind.MOVIE: "lists:movies", Entry.Kind.SHOW: "lists:shows"}
SHOW_ALL = "all"
GROUPS = [("", "No groups"), ("genre", "Genre"), ("provider", "Where to watch")]


def _fetch(request):
    return request.headers.get("X-Requested-With") == "fetch"


def _back(request, entry):
    """The page the form was sent from (the list with its filter), else the entry's list."""
    url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(url, {request.get_host()}):
        return redirect(url)
    return redirect(KIND_PAGES[entry.kind])


def filtered(entries, kind, show):
    """The entries for one filter pill, in that pill's order, and the counts for all pills."""
    counts = dict(entries.values_list("status").annotate(n=Count("id")).order_by())
    pills = [(s.value, s.label, counts.get(s.value, 0)) for s in Entry.statuses_for(kind)]
    pills.append((SHOW_ALL, "All", sum(counts.values())))
    if show == SHOW_ALL:
        rows = entries.order_by("-added_at")
    elif show == Entry.Status.WATCHED:
        rows = entries.filter(status=show).order_by("-watched_at", "-added_at")
    else:
        rows = entries.filter(status=show).order_by("-added_at")
    return rows, pills


def existing(user, kind, tmdb_id, title, year, season=None):
    """The entry already on someone's list for a title: the same TMDB title, or the same title and year.
    A show's season is a title of its own, and so is the whole show (season None)."""
    entries = user.entries.filter(kind=kind, season=season if kind == Entry.Kind.SHOW else None)
    if tmdb_id:
        found = entries.filter(tmdb_id=tmdb_id).first()
        if found:
            return found
    return entries.filter(title__iexact=title, year=year).first()


def chosen_group(request, kind):
    """How the list is grouped; remembered per list until changed."""
    key = f"group-{kind}"
    group = request.GET.get("group")
    if group is not None and group in dict(GROUPS):
        request.session[key] = group
    return request.session.get(key, "")


def grouped(rows, group, services):
    """The rows under headings: [(label, rows, yours)], yours meaning one of your streaming services.
    Each title is under one heading only: its main genre, or the first of your services that has it
    (else the most prominent one). Its row still shows all of them."""
    mine = {s.lower(): i for i, s in enumerate(services)} if group == "provider" else {}
    buckets = defaultdict(list)
    for entry in rows:
        if group == "genre":
            key = entry.genres[0] if entry.genres else ""
        elif entry.watch:
            names = entry.provider_names
            yours = [n for n in names if n.lower() in mine]
            key = min(yours, key=lambda n: mine[n.lower()]) if yours else names[0]
        else:
            key = "-" if entry.providers_checked_at else ""
        buckets[key].append(entry)
    fallback = {"": "Where to watch unknown" if group == "provider" else "No genre", "-": "Not streaming (rent or buy)"}

    def order(key):
        if key in fallback:
            return (2, key == "", key)
        if key.lower() in mine:
            return (0, mine[key.lower()], key)
        return (1, -len(buckets[key]), key)

    return [(fallback.get(key, key), buckets[key], key.lower() in mine) for key in sorted(buckets, key=order)]


TAG_ALL, TAG_NONE = "all", "none"


def chosen_tag(request, tags):
    """Which of your own groups the lists show: TAG_ALL, TAG_NONE (titles in none) or a Tag.
    Remembered for both lists until changed, as the groups are the same for movies and shows."""
    value = request.GET.get("tag")
    if value is not None:
        request.session["tag"] = value
    value = request.session.get("tag", TAG_ALL)
    if value == TAG_NONE and tags:
        return TAG_NONE
    return next((t for t in tags if str(t.pk) == value), TAG_ALL)


def tag_chips(entries, show, tags):
    """The chips for your groups, with how many titles of the chosen status each has: [(value, label, count)]."""
    if not tags:
        return []
    shown = entries if show == SHOW_ALL else entries.filter(status=show)
    counts = dict(shown.values_list("tags").annotate(n=Count("id", distinct=True)).order_by())
    return ([(TAG_ALL, "All", shown.count())] + [(str(t.pk), str(t), counts.get(t.pk, 0)) for t in tags]
            + [(TAG_NONE, "In no group", counts.get(None, 0))])


def chosen_filter(request, kind):
    show = request.GET.get("show", Entry.Status.WANT)
    valid = [s.value for s in Entry.statuses_for(kind)] + [SHOW_ALL]
    return show if show in valid else Entry.Status.WANT


@login_required
def home(request):
    """What's new from your friends since your last visit, requests and imports waiting for you,
    and what you're in the middle of."""
    me = request.user
    since = activity.visit(me)
    has_friends = Friendship.objects.filter(user=me).exists()
    watching = list(me.entries.filter(status=Entry.Status.WATCHING).order_by("-added_at")[:12])
    to_watch = dict(me.entries.filter(status=Entry.Status.WANT).values_list("kind").annotate(n=Count("id")).order_by())
    return render(request, "lists/home.html", {
        "since": since,
        "news": activity.news(me, since) if has_friends else [],
        "has_friends": has_friends,
        "incoming": incoming_requests(me),
        "watching": watching,
        "to_watch_movies": to_watch.get(Entry.Kind.MOVIE, 0),
        "to_watch_shows": to_watch.get(Entry.Kind.SHOW, 0),
        "recent": [] if watching else list(me.entries.filter(status=Entry.Status.WANT).order_by("-added_at")[:12]),
        "services": {s.lower() for s in me.all_services},
    })


@login_required
def movies(request):
    return _list(request, Entry.Kind.MOVIE)


@login_required
def shows(request):
    return _list(request, Entry.Kind.SHOW)


def _list(request, kind):
    """One of your lists. The add form at the top puts a title into the list being shown:
    one picked from the TMDB search (with its poster, genres and where to watch), or just as typed."""
    show = chosen_filter(request, kind)
    tags = list(request.user.tags.all())
    tag = chosen_tag(request, tags)
    form = AddEntryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        entry = add(request, kind, Entry.Status.WANT if show == SHOW_ALL else show, **form.cleaned_data)
        if isinstance(tag, Tag):
            entry.tags.add(tag)  # added while looking at one of your groups: it goes into that group
        return redirect(request.get_full_path())

    lookup.refresh_if_stale(request.user)
    group = chosen_group(request, kind)
    entries = request.user.entries.filter(kind=kind)
    chips = tag_chips(entries, show, tags)
    if isinstance(tag, Tag):
        entries = entries.filter(tags=tag)
    elif tag == TAG_NONE:
        entries = entries.filter(tags__isnull=True)
    rows, pills = filtered(entries, kind, show)
    rows = rows.prefetch_related("tags")
    return render(request, "lists/list.html", {
        "tag": str(tag.pk) if isinstance(tag, Tag) else tag,
        "tag_name": str(tag) if isinstance(tag, Tag) else "",
        "tag_chips": chips,
        "kind": kind,
        "kind_label": "Movies" if kind == Entry.Kind.MOVIE else "TV shows",
        "rows": rows,
        "groups": grouped(rows, group, request.user.all_services) if group else None,
        "group": group,
        "group_choices": GROUPS,
        "pills": pills,
        "show": show,
        "form": form,
        "services": {s.lower() for s in request.user.all_services},
        "search_enabled": tmdb.enabled(),
    })


def add(request, kind, status, title, year=None, tmdb_id=None, season=None):
    """Puts a title on your list, with TMDB's details when it was picked from the search."""
    season = season if kind == Entry.Kind.SHOW else None
    found = existing(request.user, kind, tmdb_id, title, year, season)
    if found:
        messages.info(request, f"{found} is already on your list ({found.get_status_display().lower()}).")
        return found
    entry = Entry(user=request.user, kind=kind, title=title, year=year, tmdb_id=tmdb_id, season=season)
    if tmdb_id and not lookup.fill(entry):
        messages.warning(request, "TMDB couldn't be reached, so the details will be added later.")
    entry.set_status(status)
    entry.save()
    messages.success(request, f"Added {entry}.")
    return entry


@login_required
def seasons(request):
    """A show's seasons, to add the whole show or one season: ?tmdb_id=136315."""
    try:
        tmdb_id = int(request.GET.get("tmdb_id", ""))
    except ValueError:
        raise Http404("No show")
    if not tmdb.enabled():
        return JsonResponse({"seasons": [], "on_list": []})
    try:
        found = tmdb.seasons(tmdb_id)
    except tmdb.TMDBError as exc:
        return JsonResponse({"seasons": [], "on_list": [], "error": str(exc)})
    on_list = list(request.user.entries.filter(kind=Entry.Kind.SHOW, tmdb_id=tmdb_id).values_list("season", flat=True))
    return JsonResponse({
        "seasons": [{k: s[k] for k in ("number", "name", "year", "episodes")} for s in found],
        "on_list": on_list,
    })


@login_required
def search(request):
    """Titles on TMDB for the search as you type: ?q=dune&kind=movie (kind optional)."""
    if not tmdb.enabled():
        return JsonResponse({"enabled": False, "results": []})
    query = request.GET.get("q", "").strip()[:100]
    kind = request.GET.get("kind") if request.GET.get("kind") in Entry.Kind.values else None
    try:
        results = tmdb.search(query, kind)[:8] if len(query) >= 2 else []
    except tmdb.TMDBError as exc:
        return JsonResponse({"enabled": True, "results": [], "error": str(exc)})
    ids = [r["tmdb_id"] for r in results]
    mine = set(request.user.entries.filter(tmdb_id__in=ids).values_list("kind", "tmdb_id"))
    return JsonResponse({"enabled": True, "results": [
        {
            "tmdb_id": r["tmdb_id"], "kind": r["kind"], "title": r["title"], "year": r["year"],
            "poster": poster_url(r["poster"], "w92"), "overview": r["overview"][:160],
            "on_list": (r["kind"], r["tmdb_id"]) in mine,
        }
        for r in results
    ]})


@login_required
def entry_edit(request, pk):
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    genre_options, provider_options, known = suggestions(request.user, entry.kind)
    logos = {p["name"].lower(): p["logo"] for p in provider_options if p["logo"]}
    seasons = []
    if entry.kind == Entry.Kind.SHOW and entry.tmdb_id and tmdb.enabled():
        try:
            seasons = tmdb.seasons(entry.tmdb_id)
        except tmdb.TMDBError:
            pass
    season_before, synced_before = entry.season, entry.providers_sync
    form = EntryForm(request.POST or None, instance=entry, logos=logos, known=known, seasons=seasons)
    if request.method == "POST" and form.is_valid():
        form.save()
        if entry.tmdb_id and (entry.season != season_before or (entry.providers_sync and not synced_before)):
            lookup.fill(entry)  # the season's poster, year and episodes; where to watch when synced again
            entry.save()
        messages.success(request, f"Saved {entry}.")
        return redirect(reverse(KIND_PAGES[entry.kind]) + f"?show={entry.status}")
    return render(request, "lists/entry_edit.html", {
        "entry": entry, "form": form, "search_enabled": tmdb.enabled(),
        "services": {s.lower() for s in request.user.all_services},
        "genre_options": [{"name": g, "logo": ""} for g in genre_options],
        "provider_options": provider_options,
        # TMDB's services while synced: shown in the field but not removable there.
        "locked_providers": entry.providers if entry.providers_sync else [],
    })


def suggestions(user, kind):
    """What the genre and where-to-watch fields suggest while typing: TMDB's genres and the streaming
    services in your country, and your other services (yours first), plus whatever you've used on your
    own titles. Also the names TMDB knows (lower case), or None when TMDB can't be reached."""
    genres, providers, known = [], [], set()
    if tmdb.enabled():
        try:
            genres = tmdb.genres(kind)
            providers = tmdb.services(user.region)
            known = {p["name"].lower() for p in providers}
        except tmdb.TMDBError:
            known = None
    used_genres, used_providers = set(), {}
    for entry_genres, tmdb_providers, own_providers in user.entries.values_list("genres", "providers", "own_providers"):
        used_genres.update(entry_genres)
        for p in tmdb_providers + own_providers:
            used_providers.setdefault(p["name"].lower(), p)
    genres = sorted(set(genres) | used_genres, key=str.lower)

    listed = {p["name"].lower() for p in providers}
    extra = [{"name": name, "logo": ""} for name in user.all_services if name.lower() not in listed]
    extra += [p for name, p in used_providers.items() if name not in listed and name not in {e["name"].lower() for e in extra}]
    mine = {s.lower(): i for i, s in enumerate(user.all_services)}
    providers = sorted(providers + extra, key=lambda p: (p["name"].lower() not in mine, mine.get(p["name"].lower(), 0)))
    return genres, [{"name": p["name"], "logo": p.get("logo", "")} for p in providers], known


@login_required
@require_POST
def entry_link(request, pk):
    """Picks the TMDB title an entry is, from the search on its page."""
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    try:
        tmdb_id = int(request.POST.get("tmdb_id", ""))
    except ValueError:
        raise Http404("No TMDB title")
    if request.POST.get("kind", entry.kind) != entry.kind:
        messages.error(request, f"Pick a {entry.get_kind_display().lower()}: this is on your {entry.get_kind_display().lower()} list.")
        return redirect("lists:entry", entry.pk)
    other = request.user.entries.filter(kind=entry.kind, tmdb_id=tmdb_id, season=entry.season).exclude(pk=entry.pk).first()
    if other:
        messages.info(request, f"{other} is already on your list.")
        return redirect("lists:entry", entry.pk)
    entry.tmdb_id = tmdb_id
    if lookup.fill(entry):
        entry.save()
        messages.success(request, f"{entry} now has its details from TMDB.")
    else:
        messages.error(request, "TMDB couldn't be reached. Please try again.")
    return redirect("lists:entry", entry.pk)


@login_required
@require_POST
def entry_refresh(request, pk):
    """Fetches the details and where to watch again (streaming catalogues change)."""
    entry = get_object_or_404(Entry, pk=pk, user=request.user, tmdb_id__isnull=False)
    if lookup.fill(entry):
        entry.save()
        messages.success(request, f"Updated {entry} from TMDB.")
    else:
        messages.error(request, "TMDB couldn't be reached. Please try again.")
    return redirect("lists:entry", entry.pk)


@login_required
@require_POST
def entry_delete(request, pk):
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    entry.delete()
    messages.success(request, f"Removed {entry} from your list.")
    return redirect(KIND_PAGES[entry.kind])


@login_required
@require_POST
def entry_rate(request, pk):
    """Stars from the list. Tapping the current rating again clears it."""
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    try:
        rating = int(request.POST.get("rating", ""))
    except ValueError:
        raise Http404("No rating")
    if not 1 <= rating <= 5:
        raise Http404("No rating")
    entry.set_rating(None if rating == entry.rating else rating)
    entry.save(update_fields=["rating", "status", "watched_at"])
    if _fetch(request):
        return JsonResponse({"rating": entry.rating, "status": entry.status})
    return _back(request, entry)


@login_required
@require_POST
def entry_status(request, pk):
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    status = request.POST.get("status")
    if status not in [s.value for s in Entry.statuses_for(entry.kind)]:
        raise Http404("No such status")
    entry.set_status(status)
    entry.save(update_fields=["status", "watched_at"])
    messages.success(request, f"{entry}: {entry.get_status_display().lower()}.")
    return _back(request, entry)


@login_required
@require_POST
def entry_copy(request, pk):
    """Puts a friend's movie or show on your own list, as one to watch."""
    source = get_object_or_404(Entry, pk=pk)
    if not can_see(request.user, source.user):
        raise Http404("Not your friend's list")
    found = existing(request.user, source.kind, source.tmdb_id, source.title, source.year, source.season)
    if found:
        messages.info(request, f"{found} is already on your list.")
    else:
        entry = Entry(
            user=request.user, kind=source.kind, title=source.title, year=source.year, tmdb_id=source.tmdb_id,
            season=source.season, season_year=source.season_year, episodes=source.episodes,
            poster_path=source.poster_path, overview=source.overview, genres=source.genres,
        )
        # Where to watch depends on the country, which may not be theirs: looked up again for yours.
        if source.tmdb_id:
            lookup.fill(entry)
        entry.save()
        messages.success(request, f"Added {source} to your list.")
    return _back(request, source)
