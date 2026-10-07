"""Daily recommendations by AI from TMDB candidates. TMDB and the AI are faked (see test_titles)."""
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from . import recommender
from .models import AIUsage, Entry, Recommendation, RecommendationSet, Tag
from .test_titles import TitlesTestCase


def picks_response(movies=(), shows=()):
    def pick(tmdb_id, title, year=None, reason="Because you liked Dune.", group=""):
        return {"tmdb_id": tmdb_id, "title": title, "year": year, "reason": reason, "group": group}

    data = {"movies": [pick(*m) for m in movies], "shows": [pick(*s) for s in shows]}
    call = SimpleNamespace(type="function_call", name="save_recommendations", arguments=json.dumps(data))
    return SimpleNamespace(output=[call], usage=SimpleNamespace(input_tokens=4000, output_tokens=800),
                           model="gpt-5.4-mini", id="r1", status="completed")


DEFAULT_PICKS = dict(
    movies=[(329865, "Arrival", 2016, "Thoughtful sci-fi like Dune.", "With my partner"),
            (438631, "Dune", 2021),  # on the list already: skipped
            (None, "Parasite", 2019, "A sharp thriller."),  # not a candidate: found by searching
            (None, "Something made up", 2020)],  # TMDB doesn't know it: skipped
    shows=[(95396, "Severance", 2022, "Mind-bending like Dune.")],
)


@override_settings(OPENAI_API_KEY="sk-test", AI_DAILY_LIMIT_USD=Decimal("0.10"))
class RecommendationTests(TitlesTestCase):
    def setUp(self):
        super().setUp()
        for patcher in [
            mock.patch.object(recommender, "start", side_effect=lambda batch: recommender.run(batch.pk)),
            mock.patch.object(recommender, "close_old_connections"),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user.services = ["Netflix"]
        self.user.save()
        self.partner = Tag.objects.create(user=self.user, name="With my partner")
        self.dune = Entry.objects.create(user=self.user, kind="movie", title="Dune", year=2021, tmdb_id=438631,
                                         status="watched", rating=5)

    def ai(self, *responses):
        return self.use_ai(*(responses or [picks_response(**DEFAULT_PICKS)]))

    def home(self):
        return self.client.get(reverse("lists:home"))

    def yesterday(self):
        RecommendationSet.objects.update(created_at=recommender.ai.today_start() - timedelta(hours=3))

    def test_the_days_recommendations(self):
        create = self.ai()
        response = self.home()
        batch = RecommendationSet.objects.get()
        self.assertEqual((batch.status, batch.requested), ("done", False))
        movies = [(r.title, r.tmdb_id, r.tag, r.reason) for r in response.context["rec_movies"]]
        self.assertEqual(movies, [("Arrival", 329865, self.partner, "Thoughtful sci-fi like Dune."),
                                  ("Parasite", 496243, None, "A sharp thriller.")])
        [severance] = response.context["rec_shows"]
        self.assertEqual(severance.provider_names if hasattr(severance, "provider_names") else
                         [p["name"] for p in severance.providers], ["Netflix", "Joyn"])
        self.assertContains(response, "Thoughtful sci-fi like Dune.")
        # What the AI was told: lists, ratings, groups, services, and TMDB's candidates.
        text = create.call_args.kwargs["input"][0]["content"]
        self.assertIn("Streaming services they have: Netflix.", text)
        self.assertIn("Their groups: With my partner.", text)
        self.assertIn("- Dune (2021), 5★", text)
        self.assertIn("tmdb_id 329865: Arrival (2016) – similar to Dune; streams on Netflix", text)
        self.assertNotIn("tmdb_id 438631", text)  # on the list: not a candidate
        self.assertEqual(AIUsage.objects.get().kind, "recommend")

    def test_what_streams_on_your_services_comes_first(self):
        # Only Severance streams on Netflix (see PROVIDERS); 6 shows suggested, 5 kept.
        shows = [(2316, "The Office", 2005), (2996, "The Office", 2001), (136315, "The Bear", 2022),
                 (90228, "Dune: Prophecy", 2024), (None, "Not on TMDB"), (95396, "Severance", 2022)]
        self.ai(picks_response(shows=shows))
        titles = [(r.title, r.year) for r in self.home().context["rec_shows"]]
        self.assertEqual(titles, [("Severance", 2022), ("The Office", 2005), ("The Office", 2001), ("The Bear", 2022),
                                  ("Dune: Prophecy", 2024)])

    def test_once_a_day_and_only_after_changes(self):
        create = self.ai(picks_response(**DEFAULT_PICKS), picks_response(**DEFAULT_PICKS))
        self.home()
        self.home()
        self.assertEqual(create.call_count, 1)  # not again the same day
        self.yesterday()
        self.home()
        self.assertEqual(create.call_count, 1)  # a new day, but nothing changed: the same ones
        self.dune.rating = 3
        self.dune.save()
        self.home()
        self.assertEqual((create.call_count, RecommendationSet.objects.count()), (2, 2))

    def test_groups_and_services_count_as_changes(self):
        create = self.ai(picks_response(**DEFAULT_PICKS), picks_response(**DEFAULT_PICKS))
        self.home()
        self.yesterday()
        self.user.services = ["Netflix", "Disney Plus"]
        self.user.save()
        self.home()
        self.assertEqual(create.call_count, 2)

    def test_new_recommendations_button(self):
        create = self.ai(picks_response(**DEFAULT_PICKS), picks_response(shows=[(2316, "The Office", 2005)]))
        self.home()
        self.client.post(reverse("lists:recommendations_new"))
        self.assertEqual(create.call_count, 2)
        latest = RecommendationSet.objects.first()
        self.assertTrue(latest.requested)
        # The ones just recommended aren't repeated.
        recent = create.call_args.kwargs["input"][0]["content"].split("Recommended recently (don't repeat):\n")[1]
        self.assertIn("- Arrival (2016)", recent.split("\n\n")[0])
        self.assertEqual([r.title for r in self.home().context["rec_shows"]], ["The Office"])

    def test_the_ai_limit_applies_to_the_button(self):
        create = self.ai()
        AIUsage.objects.create(user=self.user, kind="import", cost=Decimal("0.10"))
        response = self.client.post(reverse("lists:recommendations_new"), follow=True)
        self.assertContains(response, "today&#x27;s AI budget")
        create.assert_not_called()
        self.assertContains(self.home(), "You&#x27;ve used today&#x27;s AI budget")

    def test_no_ai_until_approved(self):
        create = self.ai()
        self.user.ai_approved = False
        self.user.save()
        self.assertContains(self.home(), "once an admin has approved it")
        create.assert_not_called()

    def test_nothing_without_titles(self):
        create = self.ai()
        Entry.objects.all().delete()
        self.assertContains(self.home(), "Add a few movies or shows you liked")
        create.assert_not_called()

    def test_add_as_seen_or_to_watch(self):
        self.ai()
        self.home()
        arrival = Recommendation.objects.get(title="Arrival")
        self.client.post(reverse("lists:recommendation_add", args=[arrival.pk]), {"status": "watched"})
        entry = self.user.entries.get(tmdb_id=329865)
        self.assertEqual((entry.status, list(entry.tags.all())), ("watched", [self.partner]))
        severance = Recommendation.objects.get(title="Severance")
        self.client.post(reverse("lists:recommendation_add", args=[severance.pk]), {"status": "want"})
        self.assertEqual(self.user.entries.get(tmdb_id=95396).status, "want")
        self.assertEqual([r.title for r in self.home().context["rec_movies"]], ["Parasite"])

    def test_not_for_me_is_never_recommended_again(self):
        create = self.ai(picks_response(**DEFAULT_PICKS), picks_response(**DEFAULT_PICKS))
        self.home()
        arrival = Recommendation.objects.get(title="Arrival")
        self.client.post(reverse("lists:recommendation_dismiss", args=[arrival.pk]))
        self.assertNotIn(arrival, self.home().context["rec_movies"])
        self.client.post(reverse("lists:recommendations_new"))
        self.assertIn("Not interested (never recommend):\n- Arrival (2016)", create.call_args.kwargs["input"][0]["content"])
        newest = RecommendationSet.objects.first()
        self.assertFalse(newest.items.filter(tmdb_id=329865).exists())  # the AI suggested it anyway: left out

    def test_other_peoples_recommendations_are_out_of_reach(self):
        self.ai()
        self.home()
        rec = Recommendation.objects.first()
        self.client.force_login(self.make_other())
        self.assertEqual(self.client.post(reverse("lists:recommendation_add", args=[rec.pk]), {"status": "want"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("lists:recommendation_dismiss", args=[rec.pk])).status_code, 404)

    def test_a_failure_is_shown_and_not_retried_by_itself_that_day(self):
        create = self.ai(SimpleNamespace(output=[], usage=None, model="gpt-5.4-mini", id="r", status="completed"))
        response = self.home()
        self.assertEqual(RecommendationSet.objects.get().status, "failed")
        self.assertContains(response, "The AI didn&#x27;t answer.")
        self.home()
        self.assertEqual(create.call_count, 1)

    def make_other(self):
        from accounts.models import User

        return User.objects.create_user(email="alex@example.com")
