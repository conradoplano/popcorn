"""Friends: adding them by email or with your invite link, seeing their lists, and making your own lists public."""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.models import User, new_token
from core.context import site_url
from lists.models import Entry
from lists.views import chosen_filter, filtered

from .models import FriendRequest, Friendship, are_friends, make_friends, unfriend
from .signals import JOIN_SESSION_KEY

logger = logging.getLogger(__name__)

KIND_SLUGS = {"movies": Entry.Kind.MOVIE, "shows": Entry.Kind.SHOW}


def _absolute(request, path):
    return request.build_absolute_uri(path)


@login_required
def friends(request):
    me = request.user
    friend_ids = Friendship.objects.filter(user=me).values("friend")
    people = (
        User.objects.filter(pk__in=friend_ids)
        .annotate(
            movies=Count("entries", filter=Q(entries__kind=Entry.Kind.MOVIE)),
            shows=Count("entries", filter=Q(entries__kind=Entry.Kind.SHOW)),
        )
        .order_by("name", "email")
    )
    incoming = FriendRequest.objects.filter(to_email__iexact=me.email).select_related("from_user")
    return render(request, "friends/friends.html", {
        "people": people,
        "incoming": [r for r in incoming if r.from_user.is_active],
        "outgoing": me.sent_requests.all(),
        "invite_url": _absolute(request, reverse("friends:join", args=[me.invite_token])),
        "public_movies_url": _absolute(request, reverse("friends:public", args=[me.public_token, "movies"])),
        "public_shows_url": _absolute(request, reverse("friends:public", args=[me.public_token, "shows"])),
    })


def friends_url():
    return reverse("friends:friends")


@login_required
@require_POST
def friend_add(request):
    """Asks someone by email. If they asked you already, you're friends straight away."""
    me = request.user
    email = request.POST.get("email", "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter a valid email address.")
        return redirect(friends_url())
    if email == me.email:
        messages.error(request, "That's your own address.")
        return redirect(friends_url())

    other = User.objects.filter(email__iexact=email, is_active=True).first()
    if other and are_friends(me, other):
        messages.info(request, f"You and {other} are already friends.")
    elif other and FriendRequest.objects.filter(from_user=other, to_email__iexact=me.email).exists():
        make_friends(me, other)
        messages.success(request, f"You and {other} are now friends: they had already asked you.")
    else:
        _, created = FriendRequest.objects.get_or_create(from_user=me, to_email=email)
        if not created:
            messages.info(request, f"You've already asked {email}; it's waiting for them to accept.")
        else:
            sent = _send_request(request, me, email, other)
            if other:
                messages.success(request, f"Asked {other}. " + ("They've been sent an email; " if sent else "")
                                 + "you'll see each other's lists once they accept.")
            else:
                messages.success(request, f"{email} doesn't use {settings.SITE_NAME} yet. "
                                 + ("We've emailed them an invitation; " if sent else "")
                                 + "you'll be friends once they create an account.")
    return redirect(friends_url())


def _send_request(request, me, email, other):
    """Tells them about the request: the Friends page if they have an account, else an invitation
    with your invite link (creating an account through it makes you friends). False if the email failed."""
    base = site_url() or request.build_absolute_uri("/").rstrip("/")
    context = {
        "inviter": me,
        "other": other,
        "url": base + (reverse("friends:friends") if other else reverse("friends:join", args=[me.invite_token])),
        "site_name": settings.SITE_NAME,
    }
    try:
        send_mail(
            subject=f"{me.get_short_name()} wants to share watchlists with you on {settings.SITE_NAME}",
            message=render_to_string("friends/email/request.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
        )
        return True
    except Exception:
        logger.exception("Could not send the friend request to %s", email)
        return False


@login_required
@require_POST
def request_accept(request, pk):
    friend_request = get_object_or_404(FriendRequest, pk=pk, to_email__iexact=request.user.email)
    other = friend_request.from_user
    make_friends(request.user, other)
    messages.success(request, f"You and {other} are now friends.")
    return redirect(friends_url())


@login_required
@require_POST
def request_decline(request, pk):
    """Turning down a request (theirs) or taking back one of yours. Nobody is told."""
    friend_request = get_object_or_404(
        FriendRequest.objects.filter(Q(to_email__iexact=request.user.email) | Q(from_user=request.user)), pk=pk
    )
    friend_request.delete()
    return redirect(friends_url())


@login_required
@require_POST
def friend_remove(request, pk):
    other = get_object_or_404(User, pk=pk)
    unfriend(request.user, other)
    messages.success(request, f"You and {other} are no longer friends.")
    return redirect(friends_url())


@login_required
@require_POST
def new_invite_link(request):
    """Old links stop working, e.g. after one was shared too widely. Existing friends stay friends."""
    request.user.invite_token = new_token()
    request.user.save(update_fields=["invite_token"])
    messages.success(request, "You have a new invite link; the old one no longer works.")
    return redirect(friends_url() + "#invite")


@login_required
@require_POST
def sharing(request):
    me = request.user
    me.movies_public = request.POST.get("movies_public") == "on"
    me.shows_public = request.POST.get("shows_public") == "on"
    me.save(update_fields=["movies_public", "shows_public"])
    messages.success(request, "Saved who can see your lists.")
    return redirect(friends_url() + "#public")


def join(request, token):
    """Someone's invite link. Signed in: you're friends now. Otherwise you sign in or register first."""
    inviter = get_object_or_404(User, invite_token=token, is_active=True)
    if request.user.is_authenticated:
        if inviter.pk == request.user.pk:
            messages.info(request, "That's your own invite link: send it to a friend.")
        elif make_friends(request.user, inviter):
            messages.success(request, f"You and {inviter} are now friends: you can see each other's lists.")
        else:
            messages.info(request, f"You and {inviter} are already friends.")
        return redirect(friends_url())
    request.session[JOIN_SESSION_KEY] = token
    return render(request, "friends/join.html", {"inviter": inviter, "next": friends_url()})


@login_required
def friend_list(request, pk, kind):
    owner = get_object_or_404(User, pk=pk, is_active=True)
    if owner.pk == request.user.pk:
        return redirect("lists:movies" if kind == "movies" else "lists:shows")
    if not are_friends(request.user, owner):
        messages.error(request, "You can only see the lists of your friends.")
        return redirect(friends_url())
    return _others_list(request, owner, kind, lambda slug: reverse("friends:friend", args=[owner.pk, slug]))


def public_list(request, token, kind):
    """A list its owner made public: anyone with the link can read it, signed in or not."""
    owner = get_object_or_404(User, public_token=token, is_active=True)
    if not owner.is_public(KIND_SLUGS[kind]):
        return render(request, "friends/not_public.html", {"owner": owner}, status=404)
    return _others_list(request, owner, kind, lambda slug: reverse("friends:public", args=[token, slug]), public=True)


def _others_list(request, owner, slug, url_for, public=False):
    """Someone else's list, read only. Signed in, you can put any of their titles on your own list."""
    kind = KIND_SLUGS[slug]
    show = chosen_filter(request, kind)
    rows, pills = filtered(owner.entries.filter(kind=kind), kind, show)
    mine = set()
    if request.user.is_authenticated:
        mine = {(t.lower(), y) for t, y in request.user.entries.filter(kind=kind).values_list("title", "year")}
    rows = list(rows)
    for row in rows:
        row.on_my_list = (row.title.lower(), row.year) in mine
    # On a public page, the other list's tab only appears if that one is public too.
    tabs = [
        (s, label, url_for(s)) for s, label in (("movies", "Movies"), ("shows", "TV shows"))
        if not public or owner.is_public(KIND_SLUGS[s])
    ]
    return render(request, "friends/others_list.html", {
        "owner": owner,
        "kind": kind,
        "slug": slug,
        "tabs": tabs,
        "rows": rows,
        "pills": pills,
        "show": show,
        "public": public,
        "can_copy": request.user.is_authenticated and request.user.pk != owner.pk,
    })
