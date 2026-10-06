"""
Friends see each other's movie and TV show lists. There are no comments or likes: only the lists.

Someone becomes a friend either by opening your invite link (sharing the link is your consent, theirs is
opening it) or by accepting the request you send to their email address.
"""
from django.conf import settings
from django.db import models, transaction


class Friendship(models.Model):
    """Stored both ways round, one row per person, so "my friends" is a simple lookup."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="friendships")
    friend = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "friend"], name="unique_friendship")]

    def __str__(self):
        return f"{self.user} → {self.friend}"


class FriendRequest(models.Model):
    """Asked by email. The address may not have an account yet: the request waits until it does."""

    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_requests")
    to_email = models.EmailField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["from_user", "to_email"], name="unique_friend_request")]

    def __str__(self):
        return f"{self.from_user} → {self.to_email}"


def are_friends(a, b):
    return Friendship.objects.filter(user=a, friend=b).exists()


@transaction.atomic
def make_friends(a, b):
    """Makes two people friends and clears any requests between them. True if they weren't friends yet."""
    _, created = Friendship.objects.get_or_create(user=a, friend=b)
    Friendship.objects.get_or_create(user=b, friend=a)
    FriendRequest.objects.filter(from_user=a, to_email__iexact=b.email).delete()
    FriendRequest.objects.filter(from_user=b, to_email__iexact=a.email).delete()
    return created


def unfriend(a, b):
    Friendship.objects.filter(user=a, friend=b).delete()
    Friendship.objects.filter(user=b, friend=a).delete()


def can_see(viewer, owner, kind):
    """Your own lists, your friends' lists, and anyone's list they made public."""
    if owner.is_public(kind):
        return True
    if not viewer.is_authenticated:
        return False
    return viewer.pk == owner.pk or are_friends(viewer, owner)
