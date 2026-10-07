from django.test import TestCase
from django.urls import reverse

from accounts.models import User

from .models import Entry


class ListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="sam@example.com", name="Sam")
        self.other = User.objects.create_user(email="alex@example.com", name="Alex")
        self.client.force_login(self.user)

    def entry(self, title="Dune", kind=Entry.Kind.MOVIE, user=None, **fields):
        return Entry.objects.create(user=user or self.user, kind=kind, title=title, **fields)

    def test_home(self):
        self.entry("Dune", status=Entry.Status.WANT)
        response = self.client.get(reverse("lists:home"))
        self.assertContains(response, "Hi Sam")
        self.assertEqual(response.context["to_watch_movies"], 1)

    def test_add_movie_goes_to_want_to_watch(self):
        response = self.client.post(reverse("lists:movies"), {"title": "  Past   Lives ", "year": "2023"})
        self.assertRedirects(response, reverse("lists:movies"))
        entry = Entry.objects.get()
        self.assertEqual((entry.title, entry.year, entry.kind, entry.status), ("Past Lives", 2023, "movie", "want"))
        self.assertIsNone(entry.watched_at)

    def test_add_on_watched_filter_adds_as_watched(self):
        self.client.post(reverse("lists:shows") + "?show=watched", {"title": "Severance"})
        entry = Entry.objects.get()
        self.assertEqual((entry.kind, entry.status), ("show", "watched"))
        self.assertIsNotNone(entry.watched_at)

    def test_watching_is_only_for_shows(self):
        self.client.post(reverse("lists:movies") + "?show=watching", {"title": "Dune"})
        self.assertEqual(Entry.objects.get().status, "want")  # unknown filter falls back to "want to watch"

    def test_adding_twice_keeps_one(self):
        self.client.post(reverse("lists:movies"), {"title": "Dune", "year": "2021"})
        self.client.post(reverse("lists:movies"), {"title": "dune", "year": "2021"})
        self.assertEqual(Entry.objects.count(), 1)

    def test_lists_only_show_your_own_entries_of_that_kind(self):
        self.entry("Mine")
        self.entry("My show", kind=Entry.Kind.SHOW)
        self.entry("Theirs", user=self.other)
        response = self.client.get(reverse("lists:movies"))
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "My show")
        self.assertNotContains(response, "Theirs")

    def test_filters_and_counts(self):
        self.entry("To see")
        watched = self.entry("Seen")
        watched.set_status(Entry.Status.WATCHED)
        watched.save()
        response = self.client.get(reverse("lists:movies") + "?show=watched")
        self.assertContains(response, "Seen")
        self.assertNotContains(response, "To see")
        self.assertEqual(response.context["pills"], [("want", "Want to watch", 1), ("watched", "Watched", 1), ("all", "All", 2)])

    def test_mark_watched(self):
        entry = self.entry()
        self.client.post(reverse("lists:entry_status", args=[entry.pk]), {"status": "watched", "next": "/movies/"})
        entry.refresh_from_db()
        self.assertEqual(entry.status, "watched")
        self.assertIsNotNone(entry.watched_at)

    def test_rating_marks_watched_and_tapping_again_clears_it(self):
        entry = self.entry()
        url = reverse("lists:entry_rate", args=[entry.pk])
        response = self.client.post(url, {"rating": "4"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.json(), {"rating": 4, "status": "watched"})
        response = self.client.post(url, {"rating": "4"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.json(), {"rating": None, "status": "watched"})

    def test_rating_must_be_one_to_five(self):
        entry = self.entry()
        self.assertEqual(self.client.post(reverse("lists:entry_rate", args=[entry.pk]), {"rating": "6"}).status_code, 404)

    def test_edit(self):
        entry = self.entry(kind=Entry.Kind.SHOW)
        response = self.client.post(reverse("lists:entry", args=[entry.pk]), {
            "title": "Dune: Prophecy", "year": "2024", "status": "watching", "rating": "3", "notes": "Episode 4",
        })
        self.assertRedirects(response, reverse("lists:shows") + "?show=watching")
        entry.refresh_from_db()
        self.assertEqual((entry.title, entry.status, entry.rating, entry.notes), ("Dune: Prophecy", "watching", 3, "Episode 4"))

    def test_cannot_touch_someone_elses_entry(self):
        entry = self.entry(user=self.other)
        self.assertEqual(self.client.get(reverse("lists:entry", args=[entry.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("lists:entry_rate", args=[entry.pk]), {"rating": "1"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("lists:entry_delete", args=[entry.pk])).status_code, 404)
        self.assertTrue(Entry.objects.filter(pk=entry.pk).exists())

    def test_delete(self):
        entry = self.entry()
        self.client.post(reverse("lists:entry_delete", args=[entry.pk]))
        self.assertFalse(Entry.objects.exists())

    def test_back_link_must_stay_on_site(self):
        entry = self.entry()
        response = self.client.post(reverse("lists:entry_status", args=[entry.pk]), {"status": "watched", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("lists:movies"))
