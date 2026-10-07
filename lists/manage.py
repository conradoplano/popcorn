"""The Manage tab: importing a list of titles, confirming what was found, and where you watch."""
import threading

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import close_old_connections
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import ai, importer, lookup, tmdb
from .forms import ImportForm
from .models import Entry, Import, PendingItem, Tag
from .views import existing


def manage_url(anchor=""):
    return reverse("lists:manage") + (f"#{anchor}" if anchor else "")


@login_required
def manage(request):
    me = request.user
    importer.expire_stale()
    form = ImportForm(request.POST or None, user=me)
    if request.method == "POST" and form.is_valid():
        job = Import.objects.create(user=me, text=form.cleaned_data["text"], add_as=form.cleaned_data["add_as"],
                                    tag=form.cleaned_data["tag"])
        importer.start(job)
        return redirect(manage_url("import"))

    pending = list(me.pending.select_related("source__tag"))
    for item in pending:
        item.choice = next((f"c{i}" for i, c in enumerate(item.candidates)
                            if c["tmdb_id"] == item.tmdb_id and c["kind"] == item.kind), "typed")
        item.options = [
            (f"c{i}", f"{c['title']} ({c['year'] or '?'}) · {Entry.Kind(c['kind']).label}") for i, c in enumerate(item.candidates)
        ] + [("typed", "None of these: keep it as typed" if item.entry_id else "None of these: add as typed")]
    region, services, regions, error = me.region, [], [], ""
    if tmdb.enabled():
        try:
            regions = tmdb.regions()
            services = tmdb.services(region)
        except tmdb.TMDBError as exc:
            error = str(exc)
    mine = {s.lower() for s in me.services}
    own_counts = {s.lower(): 0 for s in me.own_services}
    for own_providers in me.entries.exclude(own_providers=[]).values_list("own_providers", flat=True):
        for p in own_providers:
            if p["name"].lower() in own_counts:
                own_counts[p["name"].lower()] += 1
    return render(request, "lists/manage.html", {
        "form": form,
        "pending": pending,
        "unsure": sum(1 for p in pending if not p.sure),
        "running": me.imports.filter(status=Import.Status.RUNNING).first(),
        "last_import": me.imports.exclude(status=Import.Status.RUNNING).first(),
        "tmdb_enabled": tmdb.enabled(),
        "ai_blocked": ai.blocked(me),
        "region": region,
        "regions": regions,
        # Your services first, then the most popular ones in your country.
        "services": [s for s in services if s["name"].lower() in mine] + [s for s in services if s["name"].lower() not in mine],
        "my_services": mine,
        "own_services": [(s, own_counts[s.lower()]) for s in me.own_services],
        "tmdb_error": error,
        "unlinked": me.entries.filter(tmdb_id__isnull=True).count(),
        "tags": me.tags.annotate(count=Count("entries")),
        "suggestions": [s for s in SUGGESTED_TAGS if s[1].lower() not in {t.name.lower() for t in me.tags.all()}],
    })


@login_required
def import_status(request, pk):
    """Polled while an import is being read."""
    job = get_object_or_404(Import, pk=pk, user=request.user)
    return JsonResponse({"status": job.status, "finished": job.status != Import.Status.RUNNING})


# --- pending items -----------------------------------------------------------------


def _item(request, pk):
    return get_object_or_404(PendingItem, pk=pk, user=request.user)


def confirm(items):
    """Puts pending items on their lists, or for items about an entry, makes it the chosen TMDB title.
    Returns (added, matched, already there); TMDB's details follow in the background."""
    added, matched, there = [], [], 0
    for item in items:
        if item.entry_id:
            entry = item.entry
            season = (item.season or entry.season) if item.kind == Entry.Kind.SHOW else None
            if item.tmdb_id and item.user.entries.filter(
                    kind=item.kind, tmdb_id=item.tmdb_id, season=season).exclude(pk=entry.pk).exists():
                there += 1
            elif item.tmdb_id:
                entry.tmdb_id, entry.kind, entry.title, entry.year = item.tmdb_id, item.kind, item.title, item.year
                entry.season = season
                entry.poster_path = item.poster_path
                if entry.status not in Entry.statuses_for(entry.kind):
                    entry.set_status(Entry.Status.WANT)
                entry.save()
                matched.append(entry.pk)
            item.delete()  # as typed: the entry stays as it is
            continue
        season = item.season if item.kind == Entry.Kind.SHOW else None
        if existing(item.user, item.kind, item.tmdb_id, item.title, item.year, season):
            there += 1
        else:
            entry = Entry(user=item.user, kind=item.kind, title=item.title, year=item.year, season=season,
                          tmdb_id=item.tmdb_id, poster_path=item.poster_path)
            entry.set_status(item.status if item.status in Entry.statuses_for(item.kind) else Entry.Status.WANT)
            entry.save()
            if item.source_id and item.source.tag_id:
                entry.tags.add(item.source.tag_id)
            added.append(entry.pk)
        item.delete()
    if (added or matched) and tmdb.enabled():
        start_fill(added + matched)
    return len(added), len(matched), there


def start_fill(entry_ids):
    threading.Thread(target=fill_entries, args=(entry_ids,), daemon=True, name="fill-entries").start()


def fill_entries(entry_ids):
    try:
        for entry in Entry.objects.filter(pk__in=entry_ids, tmdb_id__isnull=False).select_related("user"):
            if lookup.fill(entry):
                entry.save()
    finally:
        close_old_connections()


def _confirmed_message(request, added, matched, there):
    parts = []
    if added:
        parts.append(f"Added {added} title{'s' if added != 1 else ''} to your lists.")
    if matched:
        parts.append(f"Updated {matched} title{'s' if matched != 1 else ''} on your lists.")
    if there:
        parts.append(f"{there} {'was' if there == 1 else 'were'} already on them.")
    if parts:
        messages.success(request, " ".join(parts))


@login_required
@require_POST
def pending_update(request, pk):
    """Changes which TMDB title an item is (one of its candidates, or just as typed) and how it's added."""
    item = _item(request, pk)
    choice = request.POST.get("choice", "")
    if choice == "typed":
        item.choose(None)
        parsed = importer.parse_line(item.line)
        if parsed:
            item.title, item.year = parsed["title"], parsed["year"]
        item.kind = request.POST.get("kind") if request.POST.get("kind") in Entry.Kind.values else item.kind
    elif choice.startswith("c") and choice[1:].isdigit() and int(choice[1:]) < len(item.candidates):
        item.choose(item.candidates[int(choice[1:])])
    if request.POST.get("status") in (Entry.Status.WANT, Entry.Status.WATCHED):
        item.status = request.POST["status"]
    item.sure = True  # looked at and chosen
    item.save()
    if request.POST.get("add"):
        _confirmed_message(request, *confirm([item]))
    return redirect(manage_url("pending"))


@login_required
@require_POST
def pending_pick(request, pk):
    """A different title found with the search on the item."""
    item = _item(request, pk)
    try:
        tmdb_id = int(request.POST.get("tmdb_id", ""))
    except ValueError:
        raise Http404("No TMDB title")
    kind = request.POST.get("kind")
    if kind not in Entry.Kind.values:
        raise Http404("No kind")
    found = next((c for c in item.candidates if c["tmdb_id"] == tmdb_id and c["kind"] == kind), None)
    if found is None:
        try:
            details = tmdb.details(kind, tmdb_id, request.user.region)
        except tmdb.TMDBError as exc:
            messages.error(request, str(exc))
            return redirect(manage_url("pending"))
        found = lookup.candidate(details)
        item.candidates = [found] + item.candidates
    item.choose(found)
    item.sure = True
    item.save()
    return redirect(manage_url("pending"))


@login_required
@require_POST
def pending_discard(request, pk):
    _item(request, pk).delete()
    return redirect(manage_url("pending"))


@login_required
@require_POST
def pending_all(request):
    """Adds or discards everything that's waiting."""
    items = list(request.user.pending.all())
    if request.POST.get("action") == "discard":
        PendingItem.objects.filter(pk__in=[i.pk for i in items]).delete()
        messages.success(request, f"Discarded {len(items)} title{'s' if len(items) != 1 else ''}.")
    else:
        _confirmed_message(request, *confirm(items))
    return redirect(manage_url("pending"))


# --- your groups -------------------------------------------------------------------

SUGGESTED_TAGS = [("👨‍👩‍👧", "With the kids"), ("❤️", "With my partner"), ("🙋", "Just me"), ("🍿", "Movie night with friends")]


def _tag_fields(request):
    name = " ".join(request.POST.get("name", "").split())[:40]
    emoji = "".join(request.POST.get("emoji", "").split())[:8]
    return name, emoji


@login_required
@require_POST
def tag_add(request):
    name, emoji = _tag_fields(request)
    if not name:
        messages.error(request, "Give the group a name.")
    elif request.user.tags.filter(name__iexact=name).exists():
        messages.info(request, f"You already have a group called {name}.")
    else:
        tag = Tag.objects.create(user=request.user, name=name, emoji=emoji)
        messages.success(request, f"Added the group {tag}. Put titles in it from their page, or by adding them while it's chosen above a list.")
    return redirect(manage_url("groups"))


@login_required
@require_POST
def tag_update(request, pk):
    tag = get_object_or_404(Tag, pk=pk, user=request.user)
    name, emoji = _tag_fields(request)
    if not name:
        messages.error(request, "Give the group a name.")
    elif request.user.tags.filter(name__iexact=name).exclude(pk=tag.pk).exists():
        messages.info(request, f"You already have a group called {name}.")
    else:
        tag.name, tag.emoji = name, emoji
        tag.save()
        messages.success(request, f"Saved {tag}.")
    return redirect(manage_url("groups"))


@login_required
@require_POST
def tag_delete(request, pk):
    """The titles stay on your lists; they're just no longer in this group."""
    tag = get_object_or_404(Tag, pk=pk, user=request.user)
    tag.delete()
    messages.success(request, f"Deleted the group {tag}. Its titles are still on your lists.")
    return redirect(manage_url("groups"))


# --- where to watch ----------------------------------------------------------------


@login_required
@require_POST
def watch_settings(request):
    """Your country and the streaming services you have. Changing the country looks up where to watch again."""
    me = request.user
    if "region" in request.POST:
        region = request.POST["region"].strip().upper()
        if len(region) == 2 and region.isalpha() and region != me.region:
            me.watch_region = region
            me.save(update_fields=["watch_region"])
            if tmdb.enabled():
                lookup.start_refresh(me)
            messages.success(request, "Saved your country. Where to watch is being updated for all your titles.")
        return redirect(manage_url("watch"))
    names = request.POST.getlist("services")
    seen, services = set(), []
    for name in names:
        name = " ".join(name.split())[:60]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            services.append(name)
    me.services = services
    me.save(update_fields=["services"])
    messages.success(request, "Saved your streaming services.")
    return redirect(manage_url("watch"))


@login_required
@require_POST
def own_service_add(request):
    """One of your other services, e.g. "My DVDs": then it's suggested on every title."""
    me = request.user
    name = " ".join(request.POST.get("name", "").replace(",", " ").split())[:60]
    if not name:
        messages.error(request, "Give the service a name.")
    elif name.lower() in {s.lower() for s in me.all_services}:
        messages.info(request, f"{name} is already one of your services.")
    else:
        me.own_services = me.own_services + [name]
        me.save(update_fields=["own_services"])
        messages.success(request, f"Added {name}. Pick it on a title's page under where to watch.")
    return redirect(manage_url("watch"))


@login_required
@require_POST
def own_service_remove(request):
    """Removes one of your other services, and with it from all your titles."""
    me = request.user
    name = request.POST.get("name", "").lower()
    me.own_services = [s for s in me.own_services if s.lower() != name]
    me.services = [s for s in me.services if s.lower() != name]
    me.save(update_fields=["own_services", "services"])
    changed = 0
    for entry in me.entries.exclude(own_providers=[]):
        kept = [p for p in entry.own_providers if p["name"].lower() != name]
        if len(kept) != len(entry.own_providers):
            entry.own_providers = kept
            entry.save(update_fields=["own_providers"])
            changed += 1
    messages.success(request, f"Removed {request.POST.get('name', '')}" + (f" from your services and {changed} title{'s' if changed != 1 else ''}." if changed else " from your services."))
    return redirect(manage_url("watch"))


@login_required
@require_POST
def refresh_all(request):
    """Where to watch again for all your titles, and TMDB matches for the ones typed in by hand
    (certain ones straight away, the others to check, with AI helping like in an import)."""
    if not tmdb.enabled():
        raise Http404("TMDB isn't set up")
    importer.expire_stale()
    if not request.user.imports.filter(status=Import.Status.RUNNING).exists():
        job = Import.objects.create(user=request.user, purpose=Import.Purpose.LINK, text="")
        importer.start(job)
    return redirect(manage_url())
