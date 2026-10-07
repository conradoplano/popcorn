"""TMDB search, where to watch, grouping, imports and the Manage tab. TMDB and the AI are faked."""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User

from . import ai, importer, lookup, manage, tmdb
from .models import AIUsage, Entry, Import, PendingItem, Tag


def movie(tmdb_id, title, year, popularity=10, poster="/p.jpg", original=None):
    return {"id": tmdb_id, "media_type": "movie", "title": title, "original_title": original or title,
            "release_date": f"{year}-05-01", "poster_path": poster, "overview": f"About {title}.", "popularity": popularity}


def show(tmdb_id, name, year, popularity=10, poster="/s.jpg"):
    return {"id": tmdb_id, "media_type": "tv", "name": name, "original_name": name,
            "first_air_date": f"{year}-01-10", "poster_path": poster, "overview": f"About {name}.", "popularity": popularity}


SEARCH = {
    "dune": [movie(438631, "Dune", 2021, 100), movie(841, "Dune", 1984, 20), show(90228, "Dune: Prophecy", 2024)],
    "severance": [show(95396, "Severance", 2022, 80)],
    "arrival": [movie(329865, "Arrival", 2016, 50)],
    "the office": [show(2316, "The Office", 2005, 200), show(2996, "The Office", 2001, 50)],
    "parasite": [movie(496243, "Parasite", 2019, 60, original="기생충")],
    "the bear": [show(136315, "The Bear", 2022, 70)],
    "blade runner": [movie(78, "Blade Runner", 1982, 40), movie(335984, "Blade Runner 2049", 2017, 45)],
    "blade runner 2049": [movie(335984, "Blade Runner 2049", 2017, 45)],
    "indiana jones": [movie(1573, "Indiana Jones and the Kingdom of the Crystal Skull", 2008, 30),
                      movie(87, "Indiana Jones and the Temple of Doom", 1984, 25)],
    "indiana jones and the temple of doom": [movie(87, "Indiana Jones and the Temple of Doom", 1984, 25)],
}

PROVIDERS = {
    "results": {
        "DE": {
            "flatrate": [{"provider_name": "Netflix", "logo_path": "/nf.jpg", "display_priority": 1}],
            "ads": [{"provider_name": "Joyn", "logo_path": "/joyn.jpg", "display_priority": 5}],
            "rent": [{"provider_name": "Apple TV", "logo_path": "/apple.jpg", "display_priority": 2}],
        },
        "US": {"flatrate": [{"provider_name": "Max", "logo_path": "/max.jpg", "display_priority": 1}]},
    }
}


def fake_get(path, **params):
    """Answers like the TMDB API for the titles above."""
    if path.startswith("/search/"):
        found = SEARCH.get(params["query"].lower(), [])
        wanted = {"/search/movie": "movie", "/search/tv": "tv"}.get(path)
        return {"results": [r for r in found if wanted in (None, r["media_type"])]}
    if path.startswith(("/movie/", "/tv/")):
        kind, tmdb_id = path.strip("/").split("/")
        item = next(r for results in SEARCH.values() for r in results
                    if r["id"] == int(tmdb_id) and r["media_type"] == kind)
        genres = [{"id": 1, "name": "Science Fiction"}, {"id": 2, "name": "Adventure"}] if kind == "movie" else [{"id": 3, "name": "Drama"}]
        seasons = [
            {"season_number": 0, "name": "Specials", "air_date": "2021-01-01", "episode_count": 2, "poster_path": None},
            {"season_number": 1, "name": "Season 1", "air_date": "2022-06-23", "episode_count": 8, "poster_path": "/s1.jpg",
             "overview": "The first season."},
            {"season_number": 2, "name": "Season 2", "air_date": "2023-06-22", "episode_count": 10, "poster_path": "/s2.jpg",
             "overview": "The second season."},
        ] if kind == "tv" else []
        return {**item, "genres": genres, "seasons": seasons,
                "watch/providers": PROVIDERS if item["id"] in (438631, 95396) else {"results": {}}}
    if path.startswith("/genre/"):
        return {"genres": [{"id": 1, "name": "Drama"}, {"id": 2, "name": "Comedy"}]}
    if path.startswith("/watch/providers/regions"):
        return {"results": [{"iso_3166_1": "DE", "english_name": "Germany"}, {"iso_3166_1": "US", "english_name": "United States"}]}
    if path.startswith("/watch/providers/"):
        return {"results": [
            {"provider_name": "Netflix", "logo_path": "/nf.jpg", "display_priorities": {"DE": 1}},
            {"provider_name": "Disney Plus", "logo_path": "/dp.jpg", "display_priorities": {"DE": 2}},
        ]}
    raise AssertionError(f"unexpected TMDB call {path}")


def ai_response(titles, input_tokens=1000, output_tokens=200):
    call = SimpleNamespace(type="function_call", name="save_titles", arguments=json.dumps({"titles": titles}))
    return SimpleNamespace(output=[call], usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
                           model="gpt-5.4-mini-2026-03-17", id="r1", status="completed")


@override_settings(TMDB_API_KEY="test-key", OPENAI_API_KEY="", WATCH_REGION="DE")
class TitlesTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="sam@example.com", name="Sam")
        self.client.force_login(self.user)
        self.tmdb_calls = []

        def counting_get(path, **params):
            self.tmdb_calls.append(path)
            return fake_get(path, **params)

        # Background work runs straight away so tests can see its result.
        for patcher in [
            mock.patch.object(tmdb, "_get", side_effect=counting_get),
            mock.patch.object(importer, "start", side_effect=lambda job: importer.run(job.pk)),
            mock.patch.object(manage, "start_fill", side_effect=manage.fill_entries),
            mock.patch.object(lookup, "start_refresh", side_effect=lambda user: lookup.refresh(user.pk)),
            mock.patch.object(importer, "close_old_connections"),
            mock.patch.object(manage, "close_old_connections"),
            mock.patch.object(lookup, "close_old_connections"),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_ai(self, *responses):
        """Lets the test's user use AI, answering with these responses in turn."""
        self.user.ai_approved = True
        self.user.save()
        client = SimpleNamespace(responses=SimpleNamespace(create=mock.Mock(side_effect=list(responses))))
        patcher = mock.patch.object(ai, "get_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client.responses.create

    def do_import(self, text, add_as="want", kind="both"):
        return self.client.post(reverse("lists:manage"), {"text": text, "add_as": add_as, "kind": kind})


class ParseTests(TestCase):
    def test_bullets_years_and_kinds(self):
        text = """Movies:
        1. Dune (2021)
        - Arrival, 2016
        * [x] Parasite [movie]
        TV shows
        • Severance
        The Bear S2
        Shogun - Season 1
        Blade Runner 2049
        """
        parsed = [(p["title"], p["year"], str(p["kind"])) for p in importer.parse(text)]
        self.assertEqual(parsed, [
            ("Dune", 2021, "movie"), ("Arrival", 2016, "movie"), ("Parasite", None, "movie"),
            ("Severance", None, "show"), ("The Bear", None, "show"), ("Shogun", None, "show"),
            ("Blade Runner", 2049, "show"),  # read as a year at first; see test_number_that_is_part_of_the_title
        ])

    def test_title_that_is_a_year(self):
        self.assertEqual(importer.parse_line("1917")["title"], "1917")
        self.assertEqual(importer.parse_line("1917 (2019)")["year"], 2019)

    def test_one_line_with_commas(self):
        self.assertEqual([p["title"] for p in importer.parse("Dune, Arrival; Severance")], ["Dune", "Arrival", "Severance"])

    def test_prose(self):
        self.assertTrue(importer.looks_like_prose("You have to see Dune. Also the new season of The Bear is great."))
        self.assertFalse(importer.looks_like_prose("Dune\nArrival\nSeverance"))


class MatchTests(TestCase):
    def results(self, query):
        return [tmdb._result(r) for r in SEARCH[query]]

    def test_year_picks_the_remake(self):
        match, sure = lookup.best_match("Dune", 1984, None, self.results("dune"))
        self.assertEqual((match["tmdb_id"], sure), (841, True))

    def test_without_year_the_much_more_popular_one_is_sure(self):
        match, sure = lookup.best_match("Dune", None, "movie", self.results("dune"))
        self.assertEqual((match["tmdb_id"], sure), (438631, True))
        match, sure = lookup.best_match("The Office", None, None, self.results("the office"))
        self.assertEqual((match["tmdb_id"], sure), (2316, True))

    def test_original_title_and_accents_match(self):
        self.assertTrue(lookup.best_match("기생충", None, None, self.results("parasite"))[1])
        self.assertEqual(lookup.norm("Amélie & The  Café!"), "amelie and the cafe")

    def test_different_title_is_not_sure(self):
        match, sure = lookup.best_match("Dune Prophecy series", None, None, self.results("dune"))
        self.assertFalse(sure)


class SearchAndAddTests(TitlesTestCase):
    def test_search_marks_whats_on_your_list(self):
        Entry.objects.create(user=self.user, kind="movie", title="Dune", year=2021, tmdb_id=438631)
        data = self.client.get(reverse("lists:search"), {"q": "dune", "kind": "movie"}).json()
        self.assertEqual([(r["tmdb_id"], r["on_list"]) for r in data["results"]], [(438631, True), (841, False)])
        self.assertEqual(data["results"][0]["poster"], "https://image.tmdb.org/t/p/w92/p.jpg")

    @override_settings(TMDB_API_KEY="")
    def test_search_without_tmdb(self):
        self.assertEqual(self.client.get(reverse("lists:search"), {"q": "dune"}).json(), {"enabled": False, "results": []})

    def test_adding_a_search_result_fills_in_details(self):
        self.client.post(reverse("lists:movies"), {"title": "dune", "tmdb_id": "438631"})
        entry = Entry.objects.get()
        self.assertEqual((entry.title, entry.year, entry.tmdb_id), ("Dune", 2021, 438631))
        self.assertEqual(entry.genres, ["Science Fiction", "Adventure"])
        # Streaming in Germany (subscription and with ads), not renting, not other countries.
        self.assertEqual(entry.provider_names, ["Netflix", "Joyn"])
        self.assertIsNotNone(entry.providers_checked_at)
        self.assertContains(self.client.get(reverse("lists:movies")), "https://image.tmdb.org/t/p/w154/p.jpg")

    def test_the_same_tmdb_title_is_added_once(self):
        Entry.objects.create(user=self.user, kind="movie", title="Dune (Part One)", tmdb_id=438631)
        self.client.post(reverse("lists:movies"), {"title": "Dune", "tmdb_id": "438631"})
        self.assertEqual(Entry.objects.count(), 1)

    def test_your_country_decides_where_to_watch(self):
        self.user.watch_region = "US"
        self.user.save()
        self.client.post(reverse("lists:shows"), {"title": "Severance", "tmdb_id": "95396"})
        self.assertEqual(Entry.objects.get().provider_names, ["Max"])

    def test_link_a_title_typed_by_hand(self):
        entry = Entry.objects.create(user=self.user, kind="movie", title="dune")
        self.client.post(reverse("lists:entry_link", args=[entry.pk]), {"tmdb_id": "438631", "kind": "movie"})
        entry.refresh_from_db()
        self.assertEqual((entry.title, entry.year, entry.poster_path), ("Dune", 2021, "/p.jpg"))

    def test_link_refuses_a_show_for_a_movie(self):
        entry = Entry.objects.create(user=self.user, kind="movie", title="Severance")
        self.client.post(reverse("lists:entry_link", args=[entry.pk]), {"tmdb_id": "95396", "kind": "show"})
        entry.refresh_from_db()
        self.assertIsNone(entry.tmdb_id)

    EDIT = {"title": "Dune", "year": "2021", "status": "want", "rating": "", "notes": "",
            "genre_names": "Science Fiction, Adventure", "providers_sync": "on"}

    def test_adding_a_service_keeps_tmdbs_up_to_date(self):
        self.client.post(reverse("lists:movies"), {"title": "Dune", "tmdb_id": "438631"})
        entry = Entry.objects.get()
        page = self.client.get(reverse("lists:entry", args=[entry.pk]))
        self.assertEqual([p["name"] for p in page.context["locked_providers"]], ["Netflix", "Joyn"])
        self.client.post(reverse("lists:entry", args=[entry.pk]), {**self.EDIT, "provider_names": "Cinema"})
        entry.refresh_from_db()
        self.assertTrue(entry.providers_sync)
        self.assertEqual(entry.own_providers, [{"name": "Cinema", "logo": ""}])
        self.assertEqual(entry.provider_names, ["Netflix", "Joyn", "Cinema"])
        # TMDB's change; yours stay.
        entry.providers = [{"name": "Old", "logo": ""}]
        entry.save()
        self.client.post(reverse("lists:entry_refresh", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.provider_names, ["Netflix", "Joyn", "Cinema"])
        # A name TMDB doesn't list becomes one of your other services.
        self.user.refresh_from_db()
        self.assertEqual(self.user.own_services, ["Cinema"])

    def test_stop_keeping_it_updated(self):
        self.client.post(reverse("lists:movies"), {"title": "Dune", "tmdb_id": "438631"})
        entry = Entry.objects.get()
        data = {**self.EDIT, "provider_names": "Cinema"}
        del data["providers_sync"]
        self.client.post(reverse("lists:entry", args=[entry.pk]), data)
        entry.refresh_from_db()
        self.assertFalse(entry.providers_sync)
        self.assertEqual((entry.providers, entry.provider_names), ([], ["Netflix", "Joyn", "Cinema"]))
        self.assertEqual(entry.own_providers[0], {"name": "Netflix", "logo": "/nf.jpg"})
        lookup.refresh(self.user.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.provider_names, ["Netflix", "Joyn", "Cinema"])
        # Now all of them can be changed, e.g. Joyn removed.
        self.client.post(reverse("lists:entry", args=[entry.pk]), {**data, "provider_names": "Netflix, Cinema"})
        entry.refresh_from_db()
        self.assertEqual(entry.provider_names, ["Netflix", "Cinema"])
        # Back on: TMDB's again, and yours that TMDB doesn't have.
        self.client.post(reverse("lists:entry", args=[entry.pk]), {**self.EDIT, "provider_names": "Netflix, Cinema"})
        entry.refresh_from_db()
        self.assertEqual((entry.providers_sync, entry.provider_names, entry.own_providers),
                         (True, ["Netflix", "Joyn", "Cinema"], [{"name": "Cinema", "logo": ""}]))

    def test_stop_keeping_it_updated_with_all_of_them_in_the_field(self):
        self.client.post(reverse("lists:movies"), {"title": "Dune", "tmdb_id": "438631"})
        entry = Entry.objects.get()
        data = {**self.EDIT, "provider_names": "Netflix", "providers_complete": "1"}  # Joyn removed on the page
        del data["providers_sync"]
        self.client.post(reverse("lists:entry", args=[entry.pk]), data)
        entry.refresh_from_db()
        self.assertEqual((entry.providers_sync, entry.provider_names), (False, ["Netflix"]))

    def test_suggestions_for_genres_and_where_to_watch(self):
        self.user.services = ["Disney Plus", "Cinema"]
        self.user.save()
        entry = Entry.objects.create(user=self.user, kind="movie", title="Dune", genres=["Science Fiction"],
                                     own_providers=[{"name": "My DVDs", "logo": ""}])
        page = self.client.get(reverse("lists:entry", args=[entry.pk]))
        self.assertEqual([g["name"] for g in page.context["genre_options"]], ["Comedy", "Drama", "Science Fiction"])
        # Your services first, then the others in your country, then ones typed in by hand before.
        self.assertEqual([p["name"] for p in page.context["provider_options"]], ["Disney Plus", "Cinema", "Netflix", "My DVDs"])
        self.assertContains(page, 'id="provider-options"')

    def test_a_service_entered_by_hand_gets_its_logo(self):
        entry = Entry.objects.create(user=self.user, kind="movie", title="Dune")
        self.client.post(reverse("lists:entry", args=[entry.pk]), {
            "title": "Dune", "year": "", "status": "want", "rating": "", "notes": "",
            "genre_names": "", "provider_names": "disney plus, My DVDs",
        })
        entry.refresh_from_db()
        self.assertEqual(entry.own_providers, [{"name": "disney plus", "logo": "/dp.jpg"}, {"name": "My DVDs", "logo": ""}])
        self.user.refresh_from_db()
        self.assertEqual(self.user.own_services, ["My DVDs"])  # Disney Plus is one TMDB lists

    def test_saving_without_changing_where_to_watch_keeps_it_automatic(self):
        self.client.post(reverse("lists:movies"), {"title": "Dune", "tmdb_id": "438631"})
        entry = Entry.objects.get()
        self.client.post(reverse("lists:entry", args=[entry.pk]), {
            "title": "Dune", "year": "2021", "status": "watched", "rating": "", "notes": "Great",
            "genre_names": "Science Fiction", "provider_names": "", "providers_sync": "on",
        })
        entry.refresh_from_db()
        self.assertEqual((entry.providers_sync, entry.provider_names, entry.genres),
                         (True, ["Netflix", "Joyn"], ["Science Fiction"]))

    def test_copying_a_friends_title_looks_up_your_country(self):
        from friends.models import make_friends

        friend = User.objects.create_user(email="alex@example.com", watch_region="US")
        make_friends(self.user, friend)
        theirs = Entry.objects.create(user=friend, kind="show", title="Severance", year=2022, tmdb_id=95396,
                                      providers=[{"name": "Max", "logo": ""}])
        self.client.post(reverse("lists:entry_copy", args=[theirs.pk]))
        mine = self.user.entries.get()
        self.assertEqual((mine.tmdb_id, mine.provider_names), (95396, ["Netflix", "Joyn"]))


class GroupTests(TitlesTestCase):
    def add(self, title, providers=(), genres=(), checked=True):
        return Entry.objects.create(
            user=self.user, kind="movie", title=title, genres=list(genres),
            providers=[{"name": p, "logo": ""} for p in providers],
            providers_checked_at="2026-01-01T00:00Z" if checked else None, tmdb_id=1 if checked else None,
        )

    def test_group_by_where_to_watch_puts_your_services_first(self):
        self.user.services = ["Disney Plus"]
        self.user.save()
        self.add("A", ["Netflix"])
        self.add("B", ["Netflix", "Disney Plus"])
        self.add("C", [])
        self.add("D", checked=False)
        with mock.patch.object(lookup, "refresh_if_stale"):
            response = self.client.get(reverse("lists:movies"), {"group": "provider"})
        groups = [(label, [e.title for e in entries], yours) for label, entries, yours in response.context["groups"]]
        # Each title once: B is on Netflix too, but under your service.
        self.assertEqual(groups, [
            ("Disney Plus", ["B"], True),
            ("Netflix", ["A"], False),
            ("Not streaming (rent or buy)", ["C"], False),
            ("Where to watch unknown", ["D"], False),
        ])
        # The choice is remembered.
        with mock.patch.object(lookup, "refresh_if_stale"):
            self.assertEqual(self.client.get(reverse("lists:movies")).context["group"], "provider")

    def test_group_by_genre(self):
        self.add("A", genres=["Comedy", "Drama"])
        self.add("B", genres=["Drama"])
        self.add("C")
        with mock.patch.object(lookup, "refresh_if_stale"):
            response = self.client.get(reverse("lists:movies"), {"group": "genre"})
        groups = [(label, [e.title for e in entries]) for label, entries, _ in response.context["groups"]]
        # Each title once, under its main genre.
        self.assertEqual(groups, [("Comedy", ["A"]), ("Drama", ["B"]), ("No genre", ["C"])])

    def test_stale_where_to_watch_is_refreshed_when_opening_a_list(self):
        entry = self.add("Dune", ["Old service"])
        entry.tmdb_id = 438631
        entry.save()
        self.client.get(reverse("lists:movies"))
        entry.refresh_from_db()
        self.assertEqual(entry.provider_names, ["Netflix", "Joyn"])
        # Not again for a while.
        self.tmdb_calls.clear()
        self.client.get(reverse("lists:movies"))
        self.assertEqual(self.tmdb_calls, [])


class ImportTests(TitlesTestCase):
    def test_import_finds_titles_and_waits_for_you(self):
        Entry.objects.create(user=self.user, kind="movie", title="Arrival", year=2016, tmdb_id=329865)
        self.do_import("Dune (2021)\nSeverance\nArrival\nxyzzy nonsense", add_as="watched")
        job = Import.objects.get()
        self.assertEqual((job.status, job.found, job.used_ai), ("done", 3, False))
        self.assertIn("1 was already on your lists", job.message)
        items = {p.line: p for p in PendingItem.objects.all()}
        self.assertEqual((items["Dune (2021)"].tmdb_id, items["Dune (2021)"].sure), (438631, True))
        self.assertEqual((items["Severance"].kind, items["Severance"].status), ("show", "watched"))
        self.assertEqual((items["xyzzy nonsense"].tmdb_id, items["xyzzy nonsense"].sure), (None, False))
        self.assertEqual(Entry.objects.count(), 1)  # nothing added yet

        page = self.client.get(reverse("lists:manage"))
        self.assertContains(page, "Check and add (3)")
        self.assertEqual(page.context["pending_count"], 3)

    def test_the_list_must_say_what_it_has(self):
        response = self.client.post(reverse("lists:manage"), {"text": "Dune", "add_as": "want"})
        self.assertContains(response, "Choose whether these are movies, TV shows or both.")
        self.assertFalse(Import.objects.exists())

    def test_only_tv_shows(self):
        # "Dune" alone would be the movie; in a list of shows it's the show (Dune: Prophecy is a guess to check).
        self.do_import("Dune\nSeverance\nArrival", kind="show")
        job = Import.objects.get()
        self.assertEqual(job.kind, "show")
        items = {p.line: p for p in PendingItem.objects.all()}
        self.assertEqual({p.kind for p in items.values()}, {"show"})
        self.assertEqual((items["Dune"].tmdb_id, items["Dune"].sure), (90228, False))
        self.assertTrue(all(c["kind"] == "show" for c in items["Dune"].candidates))
        self.assertEqual((items["Severance"].tmdb_id, items["Severance"].sure), (95396, True))
        self.assertEqual((items["Arrival"].tmdb_id, items["Arrival"].kind), (None, "show"))  # no movies looked for
        page = self.client.get(reverse("lists:manage"))
        self.assertNotContains(page, 'aria-label="Movie or TV show"')  # no choosing the kind when adding as typed

    def test_only_movies_ignores_seasons(self):
        self.do_import("Dune S2\nSeverance", kind="movie")
        items = {p.line: p for p in PendingItem.objects.all()}
        self.assertEqual((items["Dune S2"].kind, items["Dune S2"].season, items["Dune S2"].tmdb_id), ("movie", None, 438631))
        self.assertEqual((items["Severance"].kind, items["Severance"].tmdb_id), ("movie", None))

    def test_number_that_is_part_of_the_title(self):
        self.do_import("Blade Runner 2049\nBlade Runner (1982)")
        found = sorted(PendingItem.objects.values_list("title", "year", "sure"))
        self.assertEqual(found, [("Blade Runner", 1982, True), ("Blade Runner 2049", 2017, True)])

    def test_add_one(self):
        self.do_import("Dune (2021)")
        item = PendingItem.objects.get()
        self.client.post(reverse("lists:pending_update", args=[item.pk]), {"choice": "c0", "status": "watched", "add": "1"})
        entry = Entry.objects.get()
        self.assertEqual((entry.title, entry.status, entry.genres), ("Dune", "watched", ["Science Fiction", "Adventure"]))
        self.assertIsNotNone(entry.watched_at)
        self.assertFalse(PendingItem.objects.exists())

    def test_choose_another_candidate_or_as_typed(self):
        self.do_import("Dune")
        item = PendingItem.objects.get()
        self.assertEqual(item.tmdb_id, 438631)
        self.client.post(reverse("lists:pending_update", args=[item.pk]), {"choice": "c1", "status": "want"})
        item.refresh_from_db()
        self.assertEqual((item.tmdb_id, item.year, item.sure), (841, 1984, True))
        self.client.post(reverse("lists:pending_update", args=[item.pk]), {"choice": "typed", "kind": "show", "status": "want"})
        item.refresh_from_db()
        self.assertEqual((item.tmdb_id, item.title, item.kind, item.poster_path), (None, "Dune", "show", ""))

    def test_pick_from_search(self):
        self.do_import("xyzzy")
        item = PendingItem.objects.get()
        self.client.post(reverse("lists:pending_pick", args=[item.pk]), {"tmdb_id": "95396", "kind": "show"})
        item.refresh_from_db()
        self.assertEqual((item.title, item.kind, item.sure), ("Severance", "show", True))

    def test_add_all_and_discard_all(self):
        self.do_import("Dune (2021)\nSeverance")
        self.client.post(reverse("lists:pending_all"), {"action": "add"})
        self.assertEqual(sorted(Entry.objects.values_list("title", "kind")), [("Dune", "movie"), ("Severance", "show")])
        self.do_import("Arrival\nThe Office")
        self.client.post(reverse("lists:pending_all"), {"action": "discard"})
        self.assertFalse(PendingItem.objects.exists())
        self.assertEqual(Entry.objects.count(), 2)

    def test_importing_again_skips_whats_waiting(self):
        self.do_import("Dune (2021)")
        self.do_import("Dune 2021\nDune (2021)")
        self.assertEqual(PendingItem.objects.count(), 1)

    def test_other_peoples_items_are_out_of_reach(self):
        other = User.objects.create_user(email="alex@example.com")
        item = PendingItem.objects.create(user=other, line="Dune", title="Dune")
        self.assertEqual(self.client.post(reverse("lists:pending_update", args=[item.pk]), {"add": "1"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("lists:pending_discard", args=[item.pk])).status_code, 404)

    @override_settings(TMDB_API_KEY="")
    def test_without_tmdb_titles_are_kept_as_typed(self):
        self.do_import("- Dune (2021)\n- Severance S1")
        items = list(PendingItem.objects.order_by("pk").values_list("title", "year", "kind", "tmdb_id"))
        self.assertEqual(items, [("Dune", 2021, "movie", None), ("Severance", None, "show", None)])
        self.assertContains(self.client.get(reverse("lists:manage")), "TMDB isn't set up")


@override_settings(OPENAI_API_KEY="sk-test", AI_DAILY_LIMIT_USD=Decimal("0.10"))
class ImportAITests(TitlesTestCase):
    def test_ai_helps_with_lines_tmdb_cant_place(self):
        create = self.use_ai(ai_response([
            {"line": "Arival", "title": "Arrival", "year": 2016, "kind": "movie", "sure": True},
        ]))
        self.do_import("Dune (2021)\nArival")
        job = Import.objects.get()
        self.assertTrue(job.used_ai)
        # Only the unclear line went to the AI.
        self.assertEqual(create.call_args.kwargs["input"][0]["content"], "Arival")
        item = PendingItem.objects.get(line="Arival")
        self.assertEqual((item.title, item.tmdb_id, item.sure), ("Arrival", 329865, True))
        usage = AIUsage.objects.get()
        self.assertEqual((usage.user, usage.kind), (self.user, "import"))
        self.assertEqual(usage.cost, Decimal("0.0016"))  # 1000 × 0.75 + 200 × 4.50 per million

    def test_no_ai_when_everything_is_found(self):
        create = self.use_ai()
        self.do_import("Dune (2021)\nSeverance")
        create.assert_not_called()

    def test_ai_is_told_what_the_list_has(self):
        create = self.use_ai(ai_response([
            {"line": "Arival", "title": "Arrival", "year": 2016, "kind": "show", "season": None, "sure": True},
        ]))
        self.do_import("Arival", kind="movie")
        self.assertEqual(create.call_args.kwargs["input"][0]["content"], "All of these are movies.\n\nArival")
        item = PendingItem.objects.get()
        self.assertEqual((item.kind, item.tmdb_id), ("movie", 329865))

    def test_ai_reads_a_message(self):
        self.use_ai(ai_response([
            {"line": "Dune", "title": "Dune", "year": 2021, "kind": "movie", "sure": True},
            {"line": "the bear", "title": "The Bear", "year": 2022, "kind": "show", "sure": True},
        ]))
        self.do_import("Hey! You have to see Dune. Also the bear is great, I watched the whole thing in a weekend.")
        self.assertEqual(sorted(PendingItem.objects.values_list("title", "tmdb_id")), [("Dune", 438631), ("The Bear", 136315)])

    def test_no_ai_until_approved(self):
        create = self.use_ai()
        self.user.ai_approved = False
        self.user.save()
        self.do_import("Arival")
        create.assert_not_called()
        self.assertIn("approved", Import.objects.get().message)

    def test_no_ai_over_the_daily_limit(self):
        create = self.use_ai()
        AIUsage.objects.create(user=self.user, kind="import", cost=Decimal("0.10"))
        self.do_import("Arival")
        create.assert_not_called()
        self.assertIn("today's AI budget", Import.objects.get().message)

    def test_ai_failure_fails_the_import(self):
        self.use_ai(SimpleNamespace(output=[], usage=None, model="gpt-5.4-mini", id="r", status="completed"))
        self.do_import("Arival")
        job = Import.objects.get()
        self.assertEqual((job.status, job.message), ("failed", "The AI didn't answer."))
        self.assertContains(self.client.get(reverse("lists:manage")), "Your last import didn")


class WatchSettingsTests(TitlesTestCase):
    def test_manage_page_lists_services_for_your_country(self):
        response = self.client.get(reverse("lists:manage"))
        self.assertEqual([s["name"] for s in response.context["services"]], ["Netflix", "Disney Plus"])
        self.assertContains(response, "Germany")

    def test_save_services(self):
        self.user.own_services = ["Cinema"]
        self.user.save()
        self.client.post(reverse("lists:watch_settings"), {"services": ["Netflix", "netflix"]})
        self.user.refresh_from_db()
        self.assertEqual((self.user.services, self.user.own_services), (["Netflix"], ["Cinema"]))

    def test_other_services(self):
        self.client.post(reverse("lists:own_service_add"), {"name": " My  DVDs "})
        self.client.post(reverse("lists:own_service_add"), {"name": "my dvds"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.own_services, ["My DVDs"])
        dvd = Entry.objects.create(user=self.user, kind="movie", title="Heat",
                                   own_providers=[{"name": "My DVDs", "logo": ""}, {"name": "Cinema", "logo": ""}])
        page = self.client.get(reverse("lists:manage"))
        self.assertEqual(page.context["own_services"], [("My DVDs", 1)])
        self.client.post(reverse("lists:own_service_remove"), {"name": "My DVDs"})
        self.user.refresh_from_db()
        dvd.refresh_from_db()
        self.assertEqual((self.user.own_services, dvd.provider_names), ([], ["Cinema"]))

    def test_changing_country_updates_where_to_watch(self):
        self.user.services = ["Netflix"]
        self.user.save()
        entry = Entry.objects.create(user=self.user, kind="show", title="Severance", tmdb_id=95396)
        self.client.post(reverse("lists:watch_settings"), {"region": "us"})
        self.user.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual((self.user.watch_region, self.user.services), ("US", ["Netflix"]))
        self.assertEqual(entry.provider_names, ["Max"])



class UpdateTests(TitlesTestCase):
    """Manage → Update all my titles: where to watch again, and TMDB matches for titles typed by hand."""

    def update(self):
        self.client.post(reverse("lists:refresh_all"))
        return Import.objects.latest("pk")

    def test_certain_matches_are_linked_and_unclear_ones_wait(self):
        linked = Entry.objects.create(user=self.user, kind="movie", title="Dune", year=2021,
                                      tmdb_id=438631, providers=[{"name": "Old", "logo": ""}])
        typed = Entry.objects.create(user=self.user, kind="movie", title="arrival", year=2016)
        unclear = Entry.objects.create(user=self.user, kind="movie", title="indiana jones 2")
        job = self.update()
        self.assertEqual((job.purpose, job.status, job.found), ("link", "done", 1))
        self.assertEqual(job.message, "Updated 1 title from TMDB. Found 1 of the ones you typed in by hand. "
                                      "1 needs you to pick the right title.")
        linked.refresh_from_db()
        typed.refresh_from_db()
        self.assertEqual(linked.provider_names, ["Netflix", "Joyn"])
        self.assertEqual((typed.tmdb_id, typed.title, typed.genres), (329865, "Arrival", ["Science Fiction", "Adventure"]))
        item = PendingItem.objects.get()
        self.assertEqual((item.entry, item.line, item.sure), (unclear, "indiana jones 2", False))
        page = self.client.get(reverse("lists:manage"))
        self.assertContains(page, "On your list as “indiana jones 2”")
        self.assertContains(page, "Your last update")

    def test_choosing_a_match_updates_the_title_on_your_list(self):
        unclear = Entry.objects.create(user=self.user, kind="movie", title="indiana jones", status="watched")
        self.update()
        item = PendingItem.objects.get()
        choice = next(f"c{i}" for i, c in enumerate(item.candidates) if c["tmdb_id"] == 87)
        self.client.post(reverse("lists:pending_update", args=[item.pk]), {"choice": choice, "add": "1"})
        unclear.refresh_from_db()
        self.assertEqual((unclear.tmdb_id, unclear.title, unclear.year, unclear.status),
                         (87, "Indiana Jones and the Temple of Doom", 1984, "watched"))
        self.assertEqual(Entry.objects.count(), 1)
        self.assertFalse(PendingItem.objects.exists())

    def test_keep_as_typed(self):
        unclear = Entry.objects.create(user=self.user, kind="movie", title="indiana jones 2")
        self.update()
        item = PendingItem.objects.get()
        self.client.post(reverse("lists:pending_update", args=[item.pk]), {"choice": "typed", "add": "1"})
        unclear.refresh_from_db()
        self.assertEqual((unclear.title, unclear.tmdb_id), ("indiana jones 2", None))
        self.assertFalse(PendingItem.objects.exists())

    def test_two_titles_for_the_same_one(self):
        Entry.objects.create(user=self.user, kind="movie", title="Arrival", tmdb_id=329865)
        again = Entry.objects.create(user=self.user, kind="movie", title="arrival", year=2016)
        self.update()
        item = PendingItem.objects.get()
        self.assertEqual((item.entry, item.sure), (again, False))
        self.client.post(reverse("lists:pending_all"), {"action": "add"})
        again.refresh_from_db()
        self.assertIsNone(again.tmdb_id)  # not made a second copy of the same title

    def test_updating_again_replaces_the_earlier_suggestion(self):
        Entry.objects.create(user=self.user, kind="movie", title="indiana jones 2")
        self.update()
        self.update()
        self.assertEqual(PendingItem.objects.count(), 1)

    @override_settings(OPENAI_API_KEY="sk-test")
    def test_ai_helps_with_titles_typed_by_hand(self):
        create = self.use_ai(ai_response([
            {"line": "indiana jones 2", "title": "Indiana Jones and the Temple of Doom", "year": 1984,
             "kind": "movie", "sure": True},
        ]))
        unclear = Entry.objects.create(user=self.user, kind="movie", title="indiana jones 2")
        job = self.update()
        self.assertEqual(create.call_args.kwargs["input"][0]["content"], "indiana jones 2")
        unclear.refresh_from_db()
        self.assertEqual((unclear.tmdb_id, unclear.year, job.used_ai), (87, 1984, True))
        self.assertFalse(PendingItem.objects.exists())


class TagTests(TitlesTestCase):
    """Your own groups, e.g. "With the kids": the same for movies and shows, shown as filters above a list."""

    def setUp(self):
        super().setUp()
        self.kids = Tag.objects.create(user=self.user, name="With the kids", emoji="👪")
        self.partner = Tag.objects.create(user=self.user, name="With my partner")
        patcher = mock.patch.object(lookup, "refresh_if_stale")
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self, title, *tags, kind="movie", user=None):
        entry = Entry.objects.create(user=user or self.user, kind=kind, title=title)
        entry.tags.set(tags)
        return entry

    def titles(self, response):
        return sorted(e.title for e in response.context["rows"])

    def test_filter_a_list_by_group(self):
        self.entry("Frozen", self.kids)
        self.entry("Coco", self.kids, self.partner)
        self.entry("Heat")
        response = self.client.get(reverse("lists:movies"), {"tag": self.kids.pk})
        self.assertEqual(self.titles(response), ["Coco", "Frozen"])
        self.assertEqual(response.context["pills"][0], ("want", "Want to watch", 2))
        self.assertEqual(response.context["tag_chips"], [
            ("all", "All", 3), (str(self.kids.pk), "👪 With the kids", 2),
            (str(self.partner.pk), "With my partner", 1), ("none", "In no group", 1),
        ])
        # Remembered, also for the TV show list: the groups are the same for both.
        self.entry("Bluey", self.kids, kind="show")
        self.assertEqual(self.titles(self.client.get(reverse("lists:shows"))), ["Bluey"])
        self.assertEqual(self.titles(self.client.get(reverse("lists:movies"), {"tag": "none"})), ["Heat"])
        self.assertEqual(self.titles(self.client.get(reverse("lists:movies"), {"tag": "all"})), ["Coco", "Frozen", "Heat"])

    def test_adding_while_a_group_is_chosen_puts_it_in_the_group(self):
        self.client.get(reverse("lists:movies"), {"tag": self.kids.pk})
        self.client.post(reverse("lists:movies"), {"title": "Moana"})
        self.assertEqual(list(Entry.objects.get().tags.all()), [self.kids])

    def test_someone_elses_group_is_ignored(self):
        other = User.objects.create_user(email="alex@example.com")
        theirs = Tag.objects.create(user=other, name="Mine")
        self.entry("Heat")
        response = self.client.get(reverse("lists:movies"), {"tag": theirs.pk})
        self.assertEqual((response.context["tag"], self.titles(response)), ("all", ["Heat"]))

    def test_no_chips_without_groups(self):
        Tag.objects.all().delete()
        self.assertEqual(self.client.get(reverse("lists:movies")).context["tag_chips"], [])

    def test_choose_groups_on_a_titles_page(self):
        entry = self.entry("Coco")
        other = User.objects.create_user(email="alex@example.com")
        theirs = Tag.objects.create(user=other, name="Theirs")
        data = {"title": "Coco", "year": "", "status": "want", "rating": "", "notes": "", "genre_names": "",
                "provider_names": "", "tags": [self.kids.pk, self.partner.pk]}
        self.client.post(reverse("lists:entry", args=[entry.pk]), data)
        self.assertEqual(set(entry.tags.all()), {self.kids, self.partner})
        response = self.client.post(reverse("lists:entry", args=[entry.pk]), {**data, "tags": [theirs.pk]})
        self.assertEqual(response.status_code, 200)  # not one of your groups: the form says so
        self.assertEqual(set(entry.tags.all()), {self.kids, self.partner})

    def test_groups_are_private(self):
        from friends.models import make_friends

        friend = User.objects.create_user(email="alex@example.com")
        make_friends(self.user, friend)
        self.entry("Coco", self.kids)
        self.client.force_login(friend)
        response = self.client.get(reverse("friends:friend", args=[self.user.pk, "movies"]))
        self.assertContains(response, "Coco")
        self.assertNotContains(response, "With the kids")

    def test_import_into_a_group(self):
        self.client.post(reverse("lists:manage"), {"text": "Dune (2021)", "add_as": "want", "kind": "movie", "tag": self.partner.pk})
        self.client.post(reverse("lists:pending_all"), {"action": "add"})
        self.assertEqual(list(Entry.objects.get().tags.all()), [self.partner])

    def test_manage_groups(self):
        self.client.post(reverse("lists:tag_add"), {"name": "  Christmas ", "emoji": "🎄"})
        christmas = Tag.objects.get(name="Christmas")
        self.assertEqual(christmas.emoji, "🎄")
        self.client.post(reverse("lists:tag_add"), {"name": "christmas"})
        self.assertEqual(Tag.objects.filter(name__iexact="christmas").count(), 1)
        self.client.post(reverse("lists:tag_update", args=[christmas.pk]), {"name": "Xmas", "emoji": ""})
        christmas.refresh_from_db()
        self.assertEqual((christmas.name, christmas.emoji), ("Xmas", ""))
        coco = self.entry("Coco", christmas)
        self.client.post(reverse("lists:tag_delete", args=[christmas.pk]))
        self.assertFalse(Tag.objects.filter(pk=christmas.pk).exists())
        self.assertTrue(Entry.objects.filter(pk=coco.pk).exists())
        page = self.client.get(reverse("lists:manage"))
        self.assertContains(page, "With my partner")
        self.assertEqual([s[1] for s in page.context["suggestions"]], ["Just me", "Movie night with friends"])

    def test_other_peoples_groups_are_out_of_reach(self):
        other = User.objects.create_user(email="alex@example.com")
        theirs = Tag.objects.create(user=other, name="Theirs")
        self.assertEqual(self.client.post(reverse("lists:tag_update", args=[theirs.pk]), {"name": "x"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("lists:tag_delete", args=[theirs.pk])).status_code, 404)


class SeasonTests(TitlesTestCase):
    """A show can be on a list as the whole show (as it is now) and as any of its seasons, each its own title."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(lookup, "refresh_if_stale")
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, season=""):
        return self.client.post(reverse("lists:shows"), {"title": "The Bear", "tmdb_id": "136315", "season": season})

    def test_the_whole_show_and_a_season_are_separate_titles(self):
        self.add()
        self.add(2)
        self.add(2)
        whole, second = Entry.objects.order_by("pk")
        self.assertEqual((whole.season, whole.episodes, whole.poster_path), (None, None, "/s.jpg"))
        self.assertEqual((second.season, second.season_year, second.episodes, second.poster_path, second.overview),
                         (2, 2023, 10, "/s2.jpg", "The second season."))
        self.assertEqual(str(second), "The Bear (2022) · Season 2")
        page = self.client.get(reverse("lists:shows"))
        self.assertContains(page, "Season 2")
        self.assertContains(page, "10 episodes")

    def test_movies_have_no_seasons(self):
        self.client.post(reverse("lists:movies"), {"title": "Dune", "tmdb_id": "438631", "season": "2"})
        self.assertIsNone(Entry.objects.get().season)

    def test_seasons_to_choose_from(self):
        self.add(1)
        data = self.client.get(reverse("lists:seasons"), {"tmdb_id": "136315"}).json()
        self.assertEqual(data["seasons"], [
            {"number": 1, "name": "Season 1", "year": 2022, "episodes": 8},
            {"number": 2, "name": "Season 2", "year": 2023, "episodes": 10},
        ])
        self.assertEqual(data["on_list"], [1])

    def test_change_the_season_on_its_page(self):
        self.add()
        self.add(1)
        whole, first = Entry.objects.order_by("pk")
        data = {"title": "The Bear", "year": "2022", "status": "want", "rating": "", "notes": "",
                "genre_names": "Drama", "provider_names": "", "providers_sync": "on"}
        page = self.client.get(reverse("lists:entry", args=[whole.pk]))
        self.assertContains(page, '<option value="2">Season 2 (2023)</option>', html=True)
        response = self.client.post(reverse("lists:entry", args=[whole.pk]), {**data, "season": "1"})
        self.assertContains(response, "already on your list")
        self.client.post(reverse("lists:entry", args=[whole.pk]), {**data, "season": "2"})
        whole.refresh_from_db()
        self.assertEqual((whole.season, whole.episodes, whole.poster_path), (2, 10, "/s2.jpg"))
        self.client.post(reverse("lists:entry", args=[whole.pk]), {**data, "season": ""})
        whole.refresh_from_db()
        self.assertEqual((whole.season, whole.episodes, whole.poster_path), (None, None, "/s.jpg"))

    def test_import_a_season(self):
        Entry.objects.create(user=self.user, kind="show", title="The Bear", year=2022, tmdb_id=136315)
        self.do_import("The Bear S2\nThe Bear\nSeverance season 1")
        items = sorted(PendingItem.objects.values_list("title", "season"))
        self.assertEqual(items, [("Severance", 1), ("The Bear", 2)])  # the whole show is on the list already
        self.client.post(reverse("lists:pending_all"), {"action": "add"})
        second = Entry.objects.get(title="The Bear", season=2)
        self.assertEqual((second.episodes, second.season_year), (10, 2023))

    def test_parse_seasons(self):
        self.assertEqual(importer.parse_line("The Bear S2")["season"], 2)
        self.assertEqual(importer.parse_line("Severance - Season 3")["season"], 3)
        self.assertEqual(importer.parse_line("Dark Staffel 1")["season"], 1)
        self.assertIsNone(importer.parse_line("Severance")["season"])

    def test_a_friends_season_is_copied_as_that_season(self):
        from friends.models import make_friends

        friend = User.objects.create_user(email="alex@example.com")
        make_friends(self.user, friend)
        Entry.objects.create(user=self.user, kind="show", title="The Bear", year=2022, tmdb_id=136315)
        theirs = Entry.objects.create(user=friend, kind="show", title="The Bear", year=2022, tmdb_id=136315, season=2)
        page = self.client.get(reverse("friends:friend", args=[friend.pk, "shows"]))
        self.assertFalse(page.context["rows"][0].on_my_list)  # you have the whole show, not season 2
        self.client.post(reverse("lists:entry_copy", args=[theirs.pk]))
        self.assertEqual(sorted(self.user.entries.values_list("season", flat=True), key=lambda s: s or 0), [None, 2])
