import re

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from lists.models import Entry

from .models import FriendRequest, Friendship, are_friends, make_friends


class FriendsTests(TestCase):
    def setUp(self):
        self.sam = User.objects.create_user(email="sam@example.com", name="Sam Rivers")
        self.alex = User.objects.create_user(email="alex@example.com", name="Alex Moon")
        self.client.force_login(self.sam)

    def add(self, email):
        return self.client.post(reverse("friends:add"), {"email": email})

    # --- Adding by email ---

    def test_adding_an_existing_user_sends_a_request_they_must_accept(self):
        self.add("Alex@Example.com")
        self.assertFalse(are_friends(self.sam, self.alex))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/friends/", mail.outbox[0].body)

        self.client.force_login(self.alex)
        response = self.client.get(reverse("friends:friends"))
        self.assertContains(response, "Want to share lists with you")
        request = FriendRequest.objects.get()
        self.client.post(reverse("friends:accept", args=[request.pk]))
        self.assertTrue(are_friends(self.sam, self.alex))
        self.assertTrue(are_friends(self.alex, self.sam))
        self.assertFalse(FriendRequest.objects.exists())

    def test_asking_someone_who_asked_you_makes_friends_straight_away(self):
        FriendRequest.objects.create(from_user=self.alex, to_email="sam@example.com")
        self.add("alex@example.com")
        self.assertTrue(are_friends(self.sam, self.alex))
        self.assertEqual(len(mail.outbox), 0)

    def test_new_address_gets_an_invitation_and_the_request_waits_for_their_account(self):
        self.add("new@example.com")
        self.assertIn(f"/join/{self.sam.invite_token}/", mail.outbox[0].body)
        newbie = User.objects.create_user(email="new@example.com")
        self.client.force_login(newbie)
        self.assertContains(self.client.get(reverse("friends:friends")), "Sam Rivers")

    def test_asking_twice_sends_one_email(self):
        self.add("alex@example.com")
        self.add("alex@example.com")
        self.assertEqual(FriendRequest.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_cannot_add_yourself(self):
        self.add("sam@example.com")
        self.assertFalse(FriendRequest.objects.exists())

    def test_decline_and_cancel(self):
        mine = FriendRequest.objects.create(from_user=self.sam, to_email="x@example.com")
        theirs = FriendRequest.objects.create(from_user=self.alex, to_email="sam@example.com")
        other = FriendRequest.objects.create(from_user=self.alex, to_email="y@example.com")
        self.client.post(reverse("friends:decline", args=[mine.pk]))
        self.client.post(reverse("friends:decline", args=[theirs.pk]))
        self.assertEqual(self.client.post(reverse("friends:decline", args=[other.pk])).status_code, 404)
        self.assertEqual(list(FriendRequest.objects.all()), [other])

    def test_cannot_accept_a_request_meant_for_someone_else(self):
        request = FriendRequest.objects.create(from_user=self.alex, to_email="y@example.com")
        self.assertEqual(self.client.post(reverse("friends:accept", args=[request.pk])).status_code, 404)
        self.assertFalse(are_friends(self.sam, self.alex))

    def test_remove_works_both_ways(self):
        make_friends(self.sam, self.alex)
        self.client.post(reverse("friends:remove", args=[self.alex.pk]))
        self.assertFalse(Friendship.objects.exists())

    # --- Invite link ---

    def test_invite_link_when_signed_in(self):
        self.client.get(reverse("friends:join", args=[self.alex.invite_token]))
        self.assertTrue(are_friends(self.sam, self.alex))

    def test_own_invite_link_does_nothing(self):
        self.client.get(reverse("friends:join", args=[self.sam.invite_token]))
        self.assertFalse(Friendship.objects.exists())

    def test_invite_link_then_register(self):
        self.client.logout()
        response = self.client.get(reverse("friends:join", args=[self.sam.invite_token]))
        self.assertContains(response, "Sam Rivers invited you")
        self.client.post(reverse("accounts:register") + "?next=/friends/", {"name": "Robin", "email": "robin@example.com"})
        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertRedirects(response, reverse("friends:friends"))
        robin = User.objects.get(email="robin@example.com")
        self.assertTrue(are_friends(robin, self.sam))

    def test_invite_link_then_sign_in(self):
        self.client.logout()
        self.client.get(reverse("friends:join", args=[self.sam.invite_token]))
        self.client.force_login(self.alex)  # sends the same user_logged_in signal as the code login
        self.assertTrue(are_friends(self.alex, self.sam))

    def test_new_link_stops_the_old_one(self):
        old = self.sam.invite_token
        self.client.post(reverse("friends:new_link"))
        self.client.force_login(self.alex)
        self.assertEqual(self.client.get(reverse("friends:join", args=[old])).status_code, 404)

    # --- Seeing lists ---

    def test_friends_see_each_others_lists(self):
        Entry.objects.create(user=self.alex, kind=Entry.Kind.SHOW, title="The Bear", status=Entry.Status.WATCHING)
        url = reverse("friends:friend", args=[self.alex.pk, "shows"]) + "?show=all"
        self.assertRedirects(self.client.get(url), reverse("friends:friends"))  # not friends yet
        make_friends(self.sam, self.alex)
        response = self.client.get(url)
        self.assertContains(response, "The Bear")
        self.assertContains(response, "+ My list")

    def test_copy_a_friends_title_to_my_list(self):
        theirs = Entry.objects.create(user=self.alex, kind=Entry.Kind.MOVIE, title="Aftersun", year=2022, rating=5,
                                      status=Entry.Status.WATCHED)
        url = reverse("lists:entry_copy", args=[theirs.pk])
        self.assertEqual(self.client.post(url).status_code, 404)  # not friends
        make_friends(self.sam, self.alex)
        self.client.post(url)
        self.client.post(url)
        mine = self.sam.entries.get()
        self.assertEqual((mine.title, mine.year, mine.status, mine.rating), ("Aftersun", 2022, "want", None))

    def test_unknown_list_kind_is_not_found(self):
        self.assertEqual(self.client.get(f"/friends/{self.alex.pk}/books/").status_code, 404)
