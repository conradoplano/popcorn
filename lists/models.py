from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Entry(models.Model):
    """A movie or TV show on someone's list: one they want to watch, are watching or have watched."""

    class Kind(models.TextChoices):
        MOVIE = "movie", "Movie"
        SHOW = "show", "TV show"

    class Status(models.TextChoices):
        WANT = "want", "Want to watch"
        WATCHING = "watching", "Watching"  # TV shows only
        WATCHED = "watched", "Watched"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="entries")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    title = models.CharField(max_length=200)
    year = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1870), MaxValueValidator(2100)]
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WANT)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)], help_text="1 to 5 stars"
    )
    notes = models.TextField(blank=True)
    added_at = models.DateTimeField(default=timezone.now)
    watched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-added_at"]
        indexes = [models.Index(fields=["user", "kind", "status"])]
        verbose_name_plural = "entries"

    def __str__(self):
        return f"{self.title} ({self.year})" if self.year else self.title

    @staticmethod
    def statuses_for(kind):
        """The statuses a list offers, in the order of its filter pills."""
        if kind == Entry.Kind.SHOW:
            return [Entry.Status.WANT, Entry.Status.WATCHING, Entry.Status.WATCHED]
        return [Entry.Status.WANT, Entry.Status.WATCHED]

    def set_status(self, status):
        """Changes the status; watched_at remembers when it was first marked watched."""
        self.status = status
        if status == Entry.Status.WATCHED and self.watched_at is None:
            self.watched_at = timezone.now()
        elif status == Entry.Status.WANT:
            self.watched_at = None

    def set_rating(self, rating):
        """Rating something you haven't watched yet means you've now watched it."""
        self.rating = rating
        if rating and self.status == Entry.Status.WANT:
            self.set_status(Entry.Status.WATCHED)

    @property
    def stars(self):
        return range(1, 6)
