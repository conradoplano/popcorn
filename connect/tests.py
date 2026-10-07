"""The MCP server for assistants: OAuth (as in Chef) and the tools. TMDB is faked (see lists/test_titles)."""
import base64
import hashlib
import json
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from friends.models import make_friends
from lists import tmdb
from lists.models import Entry, Recommendation, RecommendationSet, Tag
from lists.test_titles import fake_get

from . import mcp
from .models import AuthorizationCode, Client, Connection

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class OAuthMixin:
    def register(self, **data):
        body = {"redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "none", **data}
        return self.client.post(reverse("connect:register"), json.dumps(body), content_type="application/json")

    def authorize_url(self, client_id, **params):
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "xyz",
                 "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "popcorn",
                 "resource": "http://testserver/mcp", **params}
        return reverse("connect:authorize") + "?" + "&".join(f"{k}={v}" for k, v in query.items())

    def connect(self, user):
        """The whole flow an assistant goes through; returns the token response."""
        client_id = self.register().json()["client_id"]
        self.client.force_login(user)
        allowed = self.client.post(self.authorize_url(client_id), {"allow": "1"})
        code = parse_qs(urlsplit(allowed["Location"]).query)["code"][0]
        self.client.logout()
        return self.client.post(reverse("connect:token"), {
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client_id,
            "code_verifier": VERIFIER,
        }).json() | {"client_id": client_id}


class OAuthTests(OAuthMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pat@example.com", name="Pat")

    def test_discovery(self):
        response = self.client.post("/mcp", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertIn('resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"', response["WWW-Authenticate"])
        resource = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(resource["resource"], "http://testserver/mcp")
        self.assertEqual(resource["authorization_servers"], ["http://testserver"])
        server = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(server["issuer"], "http://testserver")
        self.assertEqual(server["registration_endpoint"], "http://testserver/oauth/register")
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(server["scopes_supported"], ["popcorn"])

    def test_full_flow_and_refresh(self):
        registered = self.register()
        self.assertEqual(registered.status_code, 201)
        client_id = registered.json()["client_id"]

        # Not logged in: first the emailed-code login, then back here.
        url = self.authorize_url(client_id)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("accounts:login") + "?next="))

        self.client.force_login(self.user)
        consent = self.client.get(url)
        self.assertContains(consent, "Connect Claude?")
        self.assertContains(consent, "claude.ai")
        allowed = self.client.post(url, {"allow": "1"})
        params = parse_qs(urlsplit(allowed["Location"]).query)
        self.assertTrue(allowed["Location"].startswith(REDIRECT + "?"))
        self.assertEqual(params["state"], ["xyz"])

        token_request = {"grant_type": "authorization_code", "code": params["code"][0], "redirect_uri": REDIRECT,
                         "client_id": client_id, "code_verifier": VERIFIER}
        tokens = self.client.post(reverse("connect:token"), token_request).json()
        self.assertEqual((tokens["token_type"], tokens["expires_in"]), ("Bearer", 3600))
        self.assertTrue(tokens["access_token"].startswith("pop_at_"))
        # Codes work once.
        self.assertEqual(self.client.post(reverse("connect:token"), token_request).json()["error"], "invalid_grant")

        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).json()["result"], {})

        refreshed = self.client.post(reverse("connect:token"), {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}).json()
        self.assertNotEqual(refreshed["access_token"], tokens["access_token"])
        # The old tokens stop working.
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).status_code, 401)
        old_refresh = self.client.post(reverse("connect:token"), {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
        self.assertEqual(old_refresh.json()["error"], "invalid_grant")
        self.assertEqual(Connection.objects.count(), 1)

    def test_pkce_is_required_and_checked(self):
        client_id = self.register().json()["client_id"]
        self.client.force_login(self.user)
        refused = self.client.get(self.authorize_url(client_id, code_challenge_method="plain"))
        self.assertIn("error=invalid_request", refused["Location"])
        allowed = self.client.post(self.authorize_url(client_id), {"allow": "1"})
        code = parse_qs(urlsplit(allowed["Location"]).query)["code"][0]
        wrong = self.client.post(reverse("connect:token"), {"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                                            "redirect_uri": REDIRECT, "code_verifier": "w" * 64})
        self.assertEqual(wrong.json()["error"], "invalid_grant")

    def test_deny(self):
        client_id = self.register().json()["client_id"]
        self.client.force_login(self.user)
        denied = self.client.post(self.authorize_url(client_id), {"deny": "1"})
        self.assertIn("error=access_denied", denied["Location"])
        self.assertFalse(AuthorizationCode.objects.exists())

    def test_unknown_redirect_is_never_followed(self):
        client_id = self.register().json()["client_id"]
        self.client.force_login(self.user)
        response = self.client.get(self.authorize_url(client_id, redirect_uri="https://evil.example.com/cb"))
        self.assertContains(response, "Can't connect this app", status_code=400)

    def test_registration_rules(self):
        self.assertEqual(self.register(redirect_uris=["http://example.com/cb"]).json()["error"], "invalid_redirect_uri")
        self.assertEqual(self.register(redirect_uris=["http://localhost:6274/cb"]).status_code, 201)
        confidential = self.register(token_endpoint_auth_method="client_secret_post").json()
        self.assertIn("client_secret", confidential)
        bad = self.client.post(reverse("connect:token"), {"grant_type": "refresh_token", "client_id": confidential["client_id"], "client_secret": "nope"})
        self.assertEqual(bad.status_code, 401)
        with mock.patch("connect.oauth.REGISTRATIONS_PER_HOUR", 2):  # two registered above
            self.assertEqual(self.register().status_code, 429)

    def test_disconnect_in_manage(self):
        tokens = self.connect(self.user)
        self.client.force_login(self.user)
        page = self.client.get(reverse("lists:manage"))
        self.assertContains(page, "http://testserver/mcp")
        self.assertContains(page, "Claude")
        connection = Connection.objects.get()
        other = User.objects.create_user(email="sam@example.com")
        self.client.force_login(other)  # someone else can't
        self.assertEqual(self.client.post(reverse("connect:connection_remove", args=[connection.pk])).status_code, 404)
        self.client.force_login(self.user)
        self.client.post(reverse("connect:connection_remove", args=[connection.pk]))
        response = self.client.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        self.assertEqual(response.status_code, 401)
        self.assertIn('error="invalid_token"', response["WWW-Authenticate"])

    def test_suspended_user_and_expired_token(self):
        tokens = self.connect(self.user)
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
        User.objects.update(is_active=False)
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).status_code, 401)
        User.objects.update(is_active=True)
        Connection.objects.update(access_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post("/mcp", ping, content_type="application/json", **auth).status_code, 401)

    def test_revoke_endpoint(self):
        tokens = self.connect(self.user)
        self.client.post(reverse("connect:revoke"), {"token": tokens["refresh_token"], "client_id": tokens["client_id"]})
        self.assertFalse(Connection.objects.exists())

    def test_unused_registrations_are_cleaned_up(self):
        self.register()
        Client.objects.update(created_at=timezone.now() - timedelta(days=2))
        self.register()
        self.assertEqual(Client.objects.count(), 1)


@override_settings(TMDB_API_KEY="test-key", WATCH_REGION="DE", OPENAI_API_KEY="")
class MCPTests(OAuthMixin, TestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(tmdb, "_get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(email="pat@example.com", name="Pat")
        self.token = self.connect(self.user)["access_token"]
        self.kids = Tag.objects.create(user=self.user, name="With the kids")
        self.dune = Entry.objects.create(user=self.user, kind="movie", title="Dune", year=2021, tmdb_id=438631,
                                         status="watched", rating=5, notes="Loved the sound")
        self.bear = Entry.objects.create(user=self.user, kind="show", title="The Bear", year=2022, tmdb_id=136315,
                                         status="watching", tag=self.kids)

    def rpc(self, method, params=None, message_id=1):
        body = {"jsonrpc": "2.0", "id": message_id, "method": method, **({"params": params} if params is not None else {})}
        return self.client.post("/mcp", body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def call(self, tool, **arguments):
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments}).json()["result"]
        if result.get("isError"):
            return result["content"][0]["text"]
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def test_handshake(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}}).json()
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "popcorn")
        self.assertIn("movies and TV shows", init["result"]["instructions"])
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])
        notified = self.client.post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                    content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(notified.status_code, 202)
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)

    def test_exactly_these_tools(self):
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [
            "get_lists", "search_titles", "get_recommendations", "get_friends_activity",
            "add_title", "update_title", "remove_title", "dismiss_recommendation",
        ])
        by_name = {t["name"]: t for t in tools}
        self.assertTrue(by_name["remove_title"]["annotations"]["destructiveHint"])
        self.assertTrue(all(by_name[n]["annotations"]["readOnlyHint"] for n in
                            ["get_lists", "search_titles", "get_recommendations", "get_friends_activity"]))
        self.assertFalse(any(by_name[n]["annotations"]["readOnlyHint"] for n in
                             ["add_title", "update_title", "remove_title", "dismiss_recommendation"]))
        self.assertEqual(by_name["add_title"]["inputSchema"]["required"], ["kind"])

    def test_get_lists(self):
        everything = self.call("get_lists")
        self.assertEqual([t["title"] for t in everything["titles"]], ["The Bear", "Dune"])
        self.assertEqual(everything["groups"], ["With the kids"])
        dune = everything["titles"][1]
        self.assertEqual((dune["entry_id"], dune["status"], dune["rating"], dune["notes"], dune["group"]),
                         (self.dune.pk, "watched", 5, "Loved the sound", None))
        self.assertEqual(dune["url"], f"http://testserver/entry/{self.dune.pk}/")
        self.assertEqual([t["title"] for t in self.call("get_lists", kind="show")["titles"]], ["The Bear"])
        self.assertEqual([t["title"] for t in self.call("get_lists", group="with the KIDS")["titles"]], ["The Bear"])
        self.assertEqual([t["title"] for t in self.call("get_lists", group="none")["titles"]], ["Dune"])
        self.assertEqual([t["title"] for t in self.call("get_lists", search="sound")["titles"]], ["Dune"])
        self.assertEqual(self.call("get_lists", limit=1)["truncated"], True)
        self.assertIn("no group called", self.call("get_lists", group="Nope"))
        # Only their own.
        other = User.objects.create_user(email="sam@example.com")
        Entry.objects.create(user=other, kind="movie", title="Theirs")
        self.assertEqual(self.call("get_lists")["total"], 2)

    def test_search_titles(self):
        results = self.call("search_titles", query="dune", kind="movie")["results"]
        self.assertEqual([(r["tmdb_id"], r["year"]) for r in results], [(438631, 2021), (841, 1984)])
        self.assertEqual(results[0]["on_their_lists"], [{"entry_id": self.dune.pk, "season": None, "status": "watched"}])
        self.assertEqual(results[1]["on_their_lists"], [])
        with override_settings(TMDB_API_KEY=""):
            self.assertIn("isn't set up", self.call("search_titles", query="dune"))

    def test_add_title(self):
        added = self.call("add_title", kind="show", tmdb_id=95396, group="With the kids")["added"]
        self.assertEqual((added["title"], added["year"], added["status"], added["group"], added["where_to_watch"]),
                         ("Severance", 2022, "want", "With the kids", ["Netflix", "Joyn"]))
        season = self.call("add_title", kind="show", tmdb_id=136315, season=2, status="watched", rating=4)["added"]
        self.assertEqual((season["title"], season["season"], season["status"], season["rating"]), ("The Bear", 2, "watched", 4))
        typed = self.call("add_title", kind="movie", title="  My   home video ", year=2020)["added"]
        self.assertEqual((typed["title"], typed["tmdb_id"]), ("My home video", None))
        # Not twice, and only valid choices.
        self.assertIn("already on their list", self.call("add_title", kind="movie", tmdb_id=438631))
        self.assertIn("already on their list", self.call("add_title", kind="show", title="The Bear", tmdb_id=136315))
        self.assertIn("status must be one of", self.call("add_title", kind="movie", title="Heat", status="watching"))
        self.assertIn("title is required", self.call("add_title", kind="movie"))
        self.assertIn("rating must be", self.call("add_title", kind="movie", title="Heat", rating=7))
        self.assertEqual(Entry.objects.filter(user=self.user).count(), 5)

    def test_adding_a_recommendation_closes_it(self):
        batch = RecommendationSet.objects.create(user=self.user, status="done")
        rec = Recommendation.objects.create(batch=batch, user=self.user, kind="movie", tmdb_id=329865, title="Arrival",
                                            year=2016, reason="Thoughtful sci-fi.", tag=self.kids)
        listed = self.call("get_recommendations")["recommendations"]
        self.assertEqual(listed, [{"recommendation_id": rec.pk, "kind": "movie", "title": "Arrival", "year": 2016,
                                   "tmdb_id": 329865, "reason": "Thoughtful sci-fi.", "group": "With the kids",
                                   "where_to_watch": []}])
        self.call("add_title", kind="movie", tmdb_id=329865)
        rec.refresh_from_db()
        self.assertEqual(rec.state, "added")
        self.assertEqual(self.call("get_recommendations")["recommendations"], [])

    def test_dismiss_recommendation(self):
        batch = RecommendationSet.objects.create(user=self.user, status="done")
        rec = Recommendation.objects.create(batch=batch, user=self.user, kind="movie", title="Heat")
        self.assertEqual(self.call("dismiss_recommendation", recommendation_id=rec.pk), {"dismissed": "Heat"})
        rec.refresh_from_db()
        self.assertEqual(rec.state, "dismissed")
        other = User.objects.create_user(email="sam@example.com")
        theirs = Recommendation.objects.create(batch=RecommendationSet.objects.create(user=other), user=other, kind="movie", title="X")
        self.assertIn("No such recommendation", self.call("dismiss_recommendation", recommendation_id=theirs.pk))

    def test_update_and_remove(self):
        updated = self.call("update_title", entry_id=self.bear.pk, status="watched", rating=4, group="", notes="Yes chef")["updated"]
        self.assertEqual((updated["status"], updated["rating"], updated["group"], updated["notes"]), ("watched", 4, None, "Yes chef"))
        self.assertIsNotNone(updated["watched"])
        self.call("update_title", entry_id=self.bear.pk, rating=0)
        self.bear.refresh_from_db()
        self.assertEqual((self.bear.rating, self.bear.notes), (None, "Yes chef"))  # only what's given changes
        self.assertIn("status must be one of", self.call("update_title", entry_id=self.dune.pk, status="watching"))
        self.assertEqual(self.call("remove_title", entry_id=self.dune.pk), {"removed": "Dune (2021)"})
        self.assertIn("No such title", self.call("remove_title", entry_id=self.dune.pk))
        other = User.objects.create_user(email="sam@example.com")
        theirs = Entry.objects.create(user=other, kind="movie", title="Theirs")
        self.assertIn("No such title", self.call("update_title", entry_id=theirs.pk, rating=1))
        self.assertTrue(Entry.objects.filter(pk=theirs.pk).exists())

    def test_friends_activity(self):
        alex = User.objects.create_user(email="alex@example.com", name="Alex")
        make_friends(self.user, alex)
        Entry.objects.create(user=alex, kind="movie", title="Dune", tmdb_id=438631, added_at=timezone.now() - timedelta(days=2))
        Entry.objects.create(user=alex, kind="movie", title="Old one", added_at=timezone.now() - timedelta(days=40))
        stranger = User.objects.create_user(email="x@example.com")
        Entry.objects.create(user=stranger, kind="movie", title="Not a friend")
        news = self.call("get_friends_activity", days=7)
        [friend] = news["friends"]
        self.assertEqual(friend["friend"], "Alex")
        self.assertEqual([(t["title"], t["what"], t["on_their_lists"]) for t in friend["titles"]], [("Dune", "added", True)])
        self.assertEqual(len(self.call("get_friends_activity", days=60)["friends"][0]["titles"]), 2)
