"""Your own lists of movies and TV shows: adding, rating, marking watched, editing."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from friends.models import can_see

from .forms import AddEntryForm, EntryForm
from .models import Entry

KIND_PAGES = {Entry.Kind.MOVIE: "lists:movies", Entry.Kind.SHOW: "lists:shows"}
SHOW_ALL = "all"


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


def chosen_filter(request, kind):
    show = request.GET.get("show", Entry.Status.WANT)
    valid = [s.value for s in Entry.statuses_for(kind)] + [SHOW_ALL]
    return show if show in valid else Entry.Status.WANT


@login_required
def home(request):
    return redirect("lists:movies")


@login_required
def movies(request):
    return _list(request, Entry.Kind.MOVIE)


@login_required
def shows(request):
    return _list(request, Entry.Kind.SHOW)


def _list(request, kind):
    """One of your lists. The add form at the top puts a title into the list being shown."""
    show = chosen_filter(request, kind)
    form = AddEntryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        title, year = form.cleaned_data["title"], form.cleaned_data["year"]
        existing = request.user.entries.filter(kind=kind, title__iexact=title, year=year).first()
        if existing:
            messages.info(request, f"{existing} is already on your list ({existing.get_status_display().lower()}).")
        else:
            entry = Entry(user=request.user, kind=kind, title=title, year=year)
            entry.set_status(Entry.Status.WANT if show == SHOW_ALL else show)
            entry.save()
            messages.success(request, f"Added {entry}.")
        return redirect(request.get_full_path())

    entries = request.user.entries.filter(kind=kind)
    rows, pills = filtered(entries, kind, show)
    return render(request, "lists/list.html", {
        "kind": kind,
        "kind_label": "Movies" if kind == Entry.Kind.MOVIE else "TV shows",
        "rows": rows,
        "pills": pills,
        "show": show,
        "form": form,
        "public": request.user.is_public(kind),
    })


@login_required
def entry_edit(request, pk):
    entry = get_object_or_404(Entry, pk=pk, user=request.user)
    form = EntryForm(request.POST or None, instance=entry)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {entry}.")
        return redirect(reverse(KIND_PAGES[entry.kind]) + f"?show={entry.status}")
    return render(request, "lists/entry_edit.html", {"entry": entry, "form": form})


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
    """Puts a friend's (or a public list's) movie or show on your own list, as one to watch."""
    source = get_object_or_404(Entry, pk=pk)
    if not can_see(request.user, source.user, source.kind):
        raise Http404("Not your friend's list")
    existing = request.user.entries.filter(kind=source.kind, title__iexact=source.title, year=source.year).first()
    if existing:
        messages.info(request, f"{existing} is already on your list.")
    else:
        Entry.objects.create(user=request.user, kind=source.kind, title=source.title, year=source.year)
        messages.success(request, f"Added {source} to your list.")
    return _back(request, source)
