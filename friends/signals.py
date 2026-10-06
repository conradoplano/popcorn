from django.contrib import messages
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from accounts.models import User

from .models import make_friends

# Set by the invite link for someone who isn't signed in yet; used up by their next sign-in or registration.
JOIN_SESSION_KEY = "join_token"


@receiver(user_logged_in)
def finish_join(sender, request, user, **kwargs):
    """Whoever opened an invite link before signing in becomes that person's friend now."""
    token = request.session.pop(JOIN_SESSION_KEY, None) if request is not None else None
    if not token:
        return
    inviter = User.objects.filter(invite_token=token, is_active=True).exclude(pk=user.pk).first()
    if inviter and make_friends(user, inviter):
        messages.success(request, f"You and {inviter} are now friends: you can see each other's lists.", fail_silently=True)
