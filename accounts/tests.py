import re
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import LoginCodeRequest, User


def code_from_mail():
    return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)


class CodeLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="sam@example.com", name="Sam Rivers")

    def _request_code(self, email="sam@example.com"):
        return self.client.post(reverse("accounts:login"), {"email": email})

    def test_lists_require_login(self):
        response = self.client.get(reverse("lists:movies"))
        self.assertRedirects(response, f"{reverse('accounts:login')}?next=/movies/")

    def test_known_email_receives_code_and_can_log_in(self):
        response = self._request_code("Sam@Example.com")
        self.assertRedirects(response, reverse("accounts:verify"))
        self.assertEqual(len(mail.outbox), 1)

        response = self.client.post(reverse("accounts:verify"), {"code": code_from_mail()})
        self.assertRedirects(response, reverse("lists:home"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_unknown_email_gets_same_flow_but_no_mail(self):
        response = self._request_code("stranger@example.com")
        self.assertRedirects(response, reverse("accounts:verify"))
        self.assertEqual(len(mail.outbox), 0)
        response = self.client.post(reverse("accounts:verify"), {"code": "123456"})
        self.assertContains(response, "not correct")

    def test_wrong_code_rejected(self):
        self._request_code()
        wrong = f"{(int(code_from_mail()) + 1) % 10**6:06d}"
        response = self.client.post(reverse("accounts:verify"), {"code": wrong})
        self.assertContains(response, "not correct")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_too_many_attempts_lock_out(self):
        self._request_code()
        code = code_from_mail()
        wrong = f"{(int(code) + 1) % 10**6:06d}"
        for _ in range(5):
            self.client.post(reverse("accounts:verify"), {"code": wrong})
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_code_is_single_use(self):
        self._request_code()
        code = code_from_mail()
        self.client.post(reverse("accounts:verify"), {"code": code})
        self.client.post(reverse("accounts:logout"))
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertRedirects(response, reverse("accounts:login"))

    def test_inactive_user_cannot_log_in(self):
        self._request_code()
        code = code_from_mail()
        self.user.is_active = False
        self.user.save()
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertContains(response, "not correct")

    def test_mail_failure_shows_message(self):
        with mock.patch("accounts.views.send_mail", side_effect=OSError("smtp down")):
            response = self._request_code()
        self.assertContains(response, "couldn&#x27;t send the email")
        self.assertNotIn("pending_login", self.client.session)

    @override_settings(DEV_LOGIN=True)
    def test_dev_login_signs_in_without_code_or_mail(self):
        response = self._request_code()
        self.assertRedirects(response, reverse("lists:home"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertEqual(len(mail.outbox), 0)

    def test_codes_per_address_are_limited(self):
        for _ in range(5):
            self._request_code()
        self.assertEqual(len(mail.outbox), 5)
        response = self._request_code()
        self.assertContains(response, "Too many codes")
        self.assertEqual(len(mail.outbox), 5)
        # Unknown addresses are limited the same way, so the limit reveals nothing.
        for _ in range(5):
            self._request_code("stranger@example.com")
        self.assertContains(self._request_code("stranger@example.com"), "Too many codes")

    def test_limit_resets_after_the_window(self):
        for _ in range(5):
            self._request_code()
        LoginCodeRequest.objects.update(created_at=timezone.now() - timedelta(minutes=20))
        self.assertRedirects(self._request_code(), reverse("accounts:verify"))

    def test_next_parameter_is_respected(self):
        self.client.post(f"{reverse('accounts:login')}?next=/friends/", {"email": "sam@example.com"})
        response = self.client.post(reverse("accounts:verify"), {"code": code_from_mail()})
        self.assertRedirects(response, "/friends/")

    def test_next_parameter_must_stay_on_site(self):
        self.client.post(f"{reverse('accounts:login')}?next=https://evil.example/", {"email": "sam@example.com"})
        response = self.client.post(reverse("accounts:verify"), {"code": code_from_mail()})
        self.assertRedirects(response, reverse("lists:home"))


class RegisterTests(TestCase):
    def _register(self, email="alex@example.com", name="  Alex   Rivers "):
        return self.client.post(reverse("accounts:register"), {"name": name, "email": email})

    def test_account_is_created_only_once_the_code_is_entered(self):
        response = self._register()
        self.assertRedirects(response, reverse("accounts:verify"))
        self.assertFalse(User.objects.filter(email="alex@example.com").exists())

        response = self.client.post(reverse("accounts:verify"), {"code": code_from_mail()})
        self.assertRedirects(response, reverse("lists:home"))
        user = User.objects.get(email="alex@example.com")
        self.assertEqual(user.name, "Alex Rivers")
        self.assertFalse(user.has_usable_password())

    def test_registering_an_existing_address_just_logs_in(self):
        user = User.objects.create_user(email="alex@example.com", name="Alex")
        self._register(name="Someone Else")
        self.client.post(reverse("accounts:verify"), {"code": code_from_mail()})
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        user.refresh_from_db()
        self.assertEqual(user.name, "Alex")
        self.assertEqual(User.objects.count(), 1)

    @override_settings(REGISTRATION_OPEN=False)
    def test_registration_can_be_closed(self):
        self.assertEqual(self.client.get(reverse("accounts:register")).status_code, 404)

    def test_each_user_gets_their_own_tokens(self):
        a = User.objects.create_user(email="a@example.com")
        b = User.objects.create_user(email="b@example.com")
        self.assertNotEqual(a.invite_token, b.invite_token)
