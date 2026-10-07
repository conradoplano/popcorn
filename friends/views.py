"""Friends: adding them by email or with your invite link, and seeing their lists and what's new on them."""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.models import User, new_token
from core.context import site_url
from lists.models import Entry
from lists.views import chosen_filter, filtered

from . import activity
from .models import FriendRequest, Friendship, are_friends, incoming_requests, make_friends, unfriend
from .signals import JOIN_SESSION_KEY

logger = logging.getLogger(__name__)

KIND_SLUGS = {"movies": Entry.Kind.MOVIE, "shows": Entry.Kind.SHOW}


def _absolute(request, path):
    return request.build_absolute_uri(path)


@login_required
def friends(request):
    """Your friends, the newest activity first, with what they've added or watched since your last visit."""
    me = request.user
    since = activity.visit(me)
    people = list(activity.with_activity(User.objects.filter(pk__in=activity.friends_of(me)), since))
    for person in people:
        person.last_active = max(filter(None, [person.last_added, person.last_watched]), default=None)
    people.sort(key=lambda p: (p.new == 0, -(p.last_active.timestamp() if p.last_active else 0), str(p).lower()))
    return render(request, "friends/friends.html", {
        "people": people,
        "since": since,
        "incoming": incoming_requests(me),
        "outgoing": me.sent_requests.all(),
        "invite_url": _absolute(request, reverse("friends:join", args=[me.invite_token])),
    })


def friends_url():
    return reverse("friends:friends")


def _back(request):
    """The page the form was on (Friends or the home page)."""
    url = request.POST.get("next", "")
    return redirect(url if url_has_allowed_host_and_scheme(url, {request.get_host()}) else friends_url())


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
    return _back(request)


@login_required
@require_POST
def request_decline(request, pk):
    """Turning down a request (theirs) or taking back one of yours. Nobody is told."""
    friend_request = get_object_or_404(
        FriendRequest.objects.filter(Q(to_email__iexact=request.user.email) | Q(from_user=request.user)), pk=pk
    )
    friend_request.delete()
    return _back(request)


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
    slug = kind
    kind = KIND_SLUGS[slug]
    show = chosen_filter(request, kind)
    rows, pills = filtered(owner.entries.filter(kind=kind), kind, show)
    rows = activity.mark_mine(request.user, list(rows))
    since = activity.since(request.user)
    for row in rows:
        row.is_new = row.added_at > since or bool(row.watched_at and row.watched_at > since)
    return render(request, "friends/others_list.html", {
        "owner": owner,
        "kind": kind,
        "slug": slug,
        "tabs": [(s, label, reverse("friends:friend", args=[owner.pk, s])) for s, label in (("movies", "Movies"), ("shows", "TV shows"))],
        "rows": rows,
        "pills": pills,
        "show": show,
        "can_copy": True,
        "new_count": sum(1 for row in rows if row.is_new),
    })
