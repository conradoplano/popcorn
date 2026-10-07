"""What's new from friends since your last visit, on the home page and the Friends page."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from lists.models import Entry

from . import activity
from .models import FriendRequest, make_friends


class ActivityTests(TestCase):
    def setUp(self):
        self.sam = User.objects.create_user(email="sam@example.com", name="Sam Rivers")
        self.alex = User.objects.create_user(email="alex@example.com", name="Alex Moon")
        make_friends(self.sam, self.alex)
        self.client.force_login(self.sam)
        self.last_visit = timezone.now() - timedelta(days=2)
        User.objects.filter(pk=self.sam.pk).update(last_visit_at=self.last_visit, news_since=self.last_visit - timedelta(days=1))

    def entry(self, title, days_ago, user=None, kind="movie", watched_days_ago=None, **fields):
        entry = Entry.objects.create(user=user or self.alex, kind=kind, title=title,
                                     added_at=timezone.now() - timedelta(days=days_ago), **fields)
        if watched_days_ago is not None:
            entry.status = Entry.Status.WATCHED
            entry.watched_at = timezone.now() - timedelta(days=watched_days_ago)
            entry.save()
        return entry

    def test_a_visit_counts_from_the_visit_before(self):
        self.client.get(reverse("lists:home"))
        self.sam.refresh_from_db()
        self.assertEqual(self.sam.news_since, self.last_visit)
        # Looking around soon after keeps the same "since".
        self.client.get(reverse("lists:home"))
        self.sam.refresh_from_db()
        self.assertEqual(self.sam.news_since, self.last_visit)
        # Coming back later: what was new before isn't any more.
        User.objects.filter(pk=self.sam.pk).update(last_visit_at=timezone.now() - timedelta(hours=2))
        self.client.get(reverse("lists:home"))
        self.sam.refresh_from_db()
        self.assertGreater(self.sam.news_since, self.last_visit)

    def test_home_shows_whats_new_from_friends(self):
        self.entry("Old", days_ago=10)
        self.entry("Aftersun", days_ago=1)
        self.entry("Heat", days_ago=10, watched_days_ago=1, rating=4)
        self.entry("Mine too", days_ago=1, tmdb_id=5)
        Entry.objects.create(user=self.sam, kind="movie", title="Mine too", tmdb_id=5)
        stranger = User.objects.create_user(email="x@example.com")
        self.entry("Not a friend", days_ago=1, user=stranger)
        response = self.client.get(reverse("lists:home"))
        [group] = response.context["news"]
        self.assertEqual(group["friend"], self.alex)
        self.assertEqual((group["added"], group["watched"]), (2, 1))
        items = {entry.title: (what, entry.on_my_list) for entry, what, _ in group["items"]}
        self.assertEqual(items, {"Aftersun": ("added", False), "Heat": ("watched", False), "Mine too": ("added", True)})
        self.assertContains(response, "Wants to watch")
        self.assertContains(response, "Watched ★★★★")

    def test_home_without_friends_or_news(self):
        response = self.client.get(reverse("lists:home"))
        self.assertContains(response, "Nothing new from your friends")
        self.alex.delete()
        self.assertContains(self.client.get(reverse("lists:home")), "Invite a friend")

    def test_friends_page_shows_new_and_last_active(self):
        carla = User.objects.create_user(email="carla@example.com", name="Carla")
        make_friends(self.sam, carla)
        self.entry("Old", days_ago=10, user=carla)
        self.entry("Aftersun", days_ago=1)
        self.entry("Heat", days_ago=1, kind="show")
        response = self.client.get(reverse("friends:friends"))
        people = response.context["people"]
        self.assertEqual([(p.name, p.new, p.movies, p.shows) for p in people], [("Alex Moon", 2, 1, 1), ("Carla", 0, 1, 0)])
        self.assertContains(response, "2 new")
        self.assertContains(response, "active 1")

    def test_new_titles_are_marked_on_a_friends_list(self):
        self.entry("Old", days_ago=10)
        self.entry("Aftersun", days_ago=1)
        response = self.client.get(reverse("friends:friend", args=[self.alex.pk, "movies"]))
        self.assertEqual({e.title: e.is_new for e in response.context["rows"]}, {"Old": False, "Aftersun": True})
        self.assertEqual(response.context["new_count"], 1)

    def test_friend_requests_badge_and_answering_on_home(self):
        carla = User.objects.create_user(email="carla@example.com", name="Carla")
        request = FriendRequest.objects.create(from_user=carla, to_email="SAM@example.com")
        response = self.client.get(reverse("lists:home"))
        self.assertEqual(response.context["friend_requests"], 1)
        self.assertContains(response, 'aria-label="1 friend request"')
        self.assertContains(response, "Want to share lists with you")
        response = self.client.post(reverse("friends:accept", args=[request.pk]), {"next": reverse("lists:home")})
        self.assertRedirects(response, reverse("lists:home"))
        self.assertEqual(self.client.get(reverse("lists:home")).context["friend_requests"], 0)

    def test_requests_from_suspended_people_dont_count(self):
        carla = User.objects.create_user(email="carla@example.com", is_active=False)
        FriendRequest.objects.create(from_user=carla, to_email=self.sam.email)
        self.assertEqual(self.client.get(reverse("lists:home")).context["friend_requests"], 0)

    def test_first_visit_looks_back_two_weeks(self):
        self.sam.news_since = None
        self.sam.date_joined = timezone.now() - timedelta(days=100)
        with mock.patch.object(timezone, "now", return_value=timezone.now()):
            self.assertEqual(activity.since(self.sam), timezone.now() - activity.FIRST_VISIT_LOOKBACK)
