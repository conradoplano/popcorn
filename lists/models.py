from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Tag(models.Model):
    """One of someone's own groups, e.g. "With the kids" or "Just me". The same groups are used for
    movies and TV shows; a title can be in several. Private: friends and public lists don't show them."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField(max_length=40)
    emoji = models.CharField(max_length=8, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["user", "name"], name="unique_tag_name")]

    def __str__(self):
        return f"{self.emoji} {self.name}" if self.emoji else self.name


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
    # TV shows: one season of it, or empty for the whole show (as it is now). Each is its own title.
    season = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(500)])
    season_year = models.PositiveSmallIntegerField(null=True, blank=True)
    episodes = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WANT)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)], help_text="1 to 5 stars"
    )
    notes = models.TextField(blank=True)
    added_at = models.DateTimeField(default=timezone.now)
    watched_at = models.DateTimeField(null=True, blank=True)

    # From TMDB when the title was found there; genres and providers can also be entered by hand.
    tmdb_id = models.PositiveIntegerField("TMDB id", null=True, blank=True)
    poster_path = models.CharField(max_length=100, blank=True)
    overview = models.TextField(blank=True)
    genres = models.JSONField(default=list, blank=True)
    # Where to stream it (subscription, free or with ads) in its owner's country, from TMDB: [{"name", "logo"}, ...].
    providers = models.JSONField("where to watch (TMDB)", default=list, blank=True)
    providers_checked_at = models.DateTimeField(null=True, blank=True)
    # Added by hand, kept when TMDB's are updated: [{"name", "logo"}, ...].
    own_providers = models.JSONField("where to watch (added)", default=list, blank=True)
    # Off: TMDB's are no longer updated; they became own_providers to change freely.
    providers_sync = models.BooleanField("keep where to watch updated from TMDB", default=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="entries")

    class Meta:
        ordering = ["-added_at"]
        indexes = [models.Index(fields=["user", "kind", "status"])]
        verbose_name_plural = "entries"

    def __str__(self):
        name = f"{self.title} ({self.year})" if self.year else self.title
        return f"{name} · Season {self.season}" if self.season else name

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

    @property
    def poster_url(self):
        return poster_url(self.poster_path)

    @property
    def tmdb_url(self):
        if self.tmdb_id:
            return f"https://www.themoviedb.org/{'movie' if self.kind == Entry.Kind.MOVIE else 'tv'}/{self.tmdb_id}"
        return ""

    @property
    def watch(self):
        """Everywhere to watch it: TMDB's services, then the ones added by hand."""
        seen = {p["name"].lower() for p in self.providers}
        return self.providers + [p for p in self.own_providers if p["name"].lower() not in seen]

    @property
    def provider_names(self):
        return [p["name"] for p in self.watch]

    @property
    def search_text(self):
        """What the search box above a list looks through. Tags only on your own list (prefetched there)."""
        tags = [t.name for t in self.tags.all()] if "tags" in getattr(self, "_prefetched_objects_cache", {}) else []
        words = [self.title, str(self.year or ""), f"season {self.season}" if self.season else "", self.notes,
                 *self.genres, *self.provider_names, *tags]
        return " ".join(words).lower()

    def apply_details(self, details):
        """Fills in what TMDB knows. Title and year too, so they're spelled as TMDB has them."""
        self.tmdb_id = details["tmdb_id"]
        self.title = details["title"][:200] or self.title
        self.year = details["year"] or self.year
        self.poster_path = details["poster"]
        self.overview = details["overview"]
        self.genres = details["genres"]
        season = next((s for s in details.get("seasons", []) if s["number"] == self.season), None) if self.season else None
        self.season_year = season["year"] if season else None
        self.episodes = season["episodes"] if season else None
        if season:
            self.overview = season["overview"] or self.overview
            self.poster_path = season["poster"] or self.poster_path
        if self.providers_sync:
            self.providers = details["providers"]
            self.providers_checked_at = timezone.now()
            # Added by hand before TMDB knew about it: TMDB's is enough now.
            known = {p["name"].lower() for p in self.providers}
            self.own_providers = [p for p in self.own_providers if p["name"].lower() not in known]

    def stop_sync(self):
        """TMDB's services become ones to change by hand, and are no longer updated."""
        self.own_providers = self.watch
        self.providers = []
        self.providers_sync = False


def poster_url(path, size="w154"):
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else ""


class Import(models.Model):
    """A list of titles pasted in to be added, read in the background; what it finds waits as PendingItems.
    Or an update of someone's titles: where to watch again, and TMDB matches for the ones typed by hand."""

    class Purpose(models.TextChoices):
        ADD = "add", "Import"
        LINK = "link", "Update"

    class Status(models.TextChoices):
        RUNNING = "running", "Reading"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="imports")
    purpose = models.CharField(max_length=10, choices=Purpose.choices, default=Purpose.ADD)
    text = models.TextField()
    # Only movies or only TV shows (empty: both), as chosen when importing.
    kind = models.CharField(max_length=10, choices=Entry.Kind.choices, blank=True)
    # What the titles become once confirmed: to watch, or already watched, and in which of your groups.
    add_as = models.CharField(max_length=10, choices=Entry.Status.choices, default=Entry.Status.WANT)
    tag = models.ForeignKey(Tag, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    used_ai = models.BooleanField(default=False)
    found = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True, help_text="Why it failed, or a note about how it was read.")
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Import by {self.user} on {self.created_at:%Y-%m-%d %H:%M}"


class PendingItem(models.Model):
    """One title from an import, waiting for its owner to add it to a list or discard it. With an entry:
    the TMDB match for a title typed by hand that's already on a list, waiting to be confirmed."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pending")
    source = models.ForeignKey(Import, null=True, blank=True, on_delete=models.SET_NULL, related_name="items")
    entry = models.ForeignKey(Entry, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    line = models.CharField("imported text", max_length=300)
    kind = models.CharField(max_length=10, choices=Entry.Kind.choices, default=Entry.Kind.MOVIE)
    title = models.CharField(max_length=200)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    season = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Entry.Status.choices, default=Entry.Status.WANT)
    # The chosen TMDB match (empty: added just as typed), and the others to choose from:
    # [{"tmdb_id", "kind", "title", "year", "poster", "overview"}, ...]
    tmdb_id = models.PositiveIntegerField(null=True, blank=True)
    poster_path = models.CharField(max_length=100, blank=True)
    candidates = models.JSONField(default=list, blank=True)
    # False when the match is a guess that its owner should check.
    sure = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["sure", "created_at", "pk"]

    def __str__(self):
        name = f"{self.title} ({self.year})" if self.year else self.title
        return f"{name} · Season {self.season}" if self.season else name

    @property
    def poster_url(self):
        return poster_url(self.poster_path, "w92")

    def choose(self, candidate):
        """Uses one of the candidates as the match, or None to add it just as typed."""
        if candidate is None:
            self.tmdb_id, self.poster_path = None, ""
            return
        self.tmdb_id = candidate["tmdb_id"]
        if candidate["kind"] != Entry.Kind.SHOW:
            self.season = None
        self.kind = candidate["kind"]
        self.title = candidate["title"][:200]
        self.year = candidate["year"]
        self.poster_path = candidate["poster"]


class AIUsage(models.Model):
    """What one call to the AI cost, for the daily limits and the admin page."""

    class Kind(models.TextChoices):
        IMPORT = "import", "Import"
        RECOMMEND = "recommend", "Recommendations"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="ai_usage")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    model = models.CharField(max_length=50, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField("cost (USD)", max_digits=8, decimal_places=4, default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI usage"
        verbose_name_plural = "AI usage"

    def __str__(self):
        return f"{self.get_kind_display()} for {self.user}: ${self.cost}"


class RecommendationSet(models.Model):
    """One day's recommendations for someone, made by AI from their lists, groups and services.
    fingerprint: their lists when it was made; a new day only brings new ones when it changed."""

    class Status(models.TextChoices):
        RUNNING = "running", "Choosing"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recommendation_sets")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    fingerprint = models.CharField(max_length=64, blank=True)
    # Asked for with the "New recommendations" button, rather than the day's own.
    requested = models.BooleanField(default=False)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Recommendations for {self.user} on {self.created_at:%Y-%m-%d %H:%M}"


class Recommendation(models.Model):
    """A movie or show recommended to someone: added to a list, turned down, or still open."""

    class State(models.TextChoices):
        OPEN = "open", "Open"
        ADDED = "added", "Added"
        DISMISSED = "dismissed", "Not for me"

    batch = models.ForeignKey(RecommendationSet, on_delete=models.CASCADE, related_name="items")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recommendations")
    kind = models.CharField(max_length=10, choices=Entry.Kind.choices)
    tmdb_id = models.PositiveIntegerField(null=True, blank=True)
    title = models.CharField(max_length=200)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    poster_path = models.CharField(max_length=100, blank=True)
    overview = models.TextField(blank=True)
    reason = models.CharField(max_length=300, blank=True)
    # The group it suits, one of its owner's.
    tag = models.ForeignKey(Tag, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    providers = models.JSONField(default=list, blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def poster_url(self):
        return poster_url(self.poster_path)
