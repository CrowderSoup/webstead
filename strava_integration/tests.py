from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from blog.models import Post
from core.models import SiteConfiguration
from files.gpx import anonymize_gpx, GpxAnonymizeOptions
from strava_integration.gpx import streams_to_gpx
from strava_integration.importer import _activity_summary, _build_mf2, import_activity
from strava_integration.models import StravaAccount, StravaActivity
from strava_integration.tasks import handle_strava_webhook_event


def _make_account(**overrides):
    defaults = {
        "athlete_id": "42",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_at": timezone.now() + timedelta(hours=6),
        "username": "alice",
        "firstname": "Alice",
        "lastname": "Athlete",
    }
    defaults.update(overrides)
    return StravaAccount.objects.create(**defaults)


def _activity_payload(**overrides):
    payload = {
        "id": 1001,
        "name": "Morning Run",
        "description": "Felt good.",
        "type": "Run",
        "sport_type": "Run",
        "start_date": "2026-01-15T08:00:00Z",
        "private": False,
    }
    payload.update(overrides)
    return payload


class StreamsToGpxTests(TestCase):
    def test_produces_parseable_gpx_accepted_by_anonymize_gpx(self):
        latlng = [[45.0, -122.0], [45.001, -122.001], [45.002, -122.002]]
        altitude = [10.0, 12.0, 15.0]
        time_stream = [0, 5, 10]
        start_date = timezone.now()

        gpx_bytes = streams_to_gpx(latlng, altitude, time_stream, start_date, name="Test Track")

        # Should round-trip through the existing anonymizer without error.
        result = anonymize_gpx(gpx_bytes, GpxAnonymizeOptions(trim_enabled=False))
        self.assertIn(b"trkpt", result)

    def test_raises_without_latlng(self):
        with self.assertRaises(ValueError):
            streams_to_gpx([], None, None, timezone.now())


class ActivitySummaryTests(TestCase):
    def test_imperial_summary_formats_distance_duration_pace_elevation(self):
        activity = _activity_payload(
            sport_type="Run",
            distance=8368.6,  # ~5.2 mi
            moving_time=2531,  # 42:11
            total_elevation_gain=95.1,  # ~312 ft
        )
        summary = _activity_summary(activity, units=SiteConfiguration.ACTIVITY_UNITS_IMPERIAL)
        self.assertEqual(summary, "Ran 5.2 mi in 42:11 (8:07/mi pace), 312 ft elevation gain.")

    def test_metric_summary_formats_distance_duration_pace_elevation(self):
        activity = _activity_payload(
            sport_type="Ride",
            distance=20000,  # 20 km
            moving_time=3600,  # 1:00:00
            total_elevation_gain=150,
        )
        summary = _activity_summary(activity, units=SiteConfiguration.ACTIVITY_UNITS_METRIC)
        self.assertEqual(summary, "Rode 20.0 km in 1:00:00 (3:00/km pace), 150 m elevation gain.")

    def test_no_distance_or_elevation_falls_back_to_verb_only(self):
        activity = _activity_payload(sport_type="Workout", distance=0, moving_time=0, total_elevation_gain=0)
        summary = _activity_summary(activity, units=SiteConfiguration.ACTIVITY_UNITS_IMPERIAL)
        self.assertEqual(summary, "Did a Workout.")

    def test_unknown_sport_type_still_produces_a_sentence(self):
        activity = _activity_payload(sport_type="Velomobile", distance=0, moving_time=0, total_elevation_gain=0)
        summary = _activity_summary(activity, units=SiteConfiguration.ACTIVITY_UNITS_IMPERIAL)
        self.assertEqual(summary, "Did a Velomobile.")


class BuildMf2Tests(TestCase):
    def test_includes_extended_properties(self):
        activity = _activity_payload(
            distance=8368.6,
            moving_time=2531,
            elapsed_time=2600,
            total_elevation_gain=95.1,
            average_speed=3.3,
            max_speed=4.1,
            has_heartrate=True,
            average_heartrate=152.3,
            max_heartrate=178,
            start_latlng=[45.0, -122.0],
            end_latlng=[45.01, -122.01],
            kudos_count=7,
        )
        mf2 = _build_mf2(activity, track_url="")
        properties = mf2["activity"][0]["properties"]

        self.assertEqual(properties["x-distance"], ["8368.6"])
        self.assertEqual(properties["x-moving-time"], ["2531"])
        self.assertEqual(properties["x-elapsed-time"], ["2600"])
        self.assertEqual(properties["x-total-elevation-gain"], ["95.1"])
        self.assertEqual(properties["x-average-speed"], ["3.3"])
        self.assertEqual(properties["x-max-speed"], ["4.1"])
        self.assertEqual(properties["x-average-heartrate"], ["152.3"])
        self.assertEqual(properties["x-max-heartrate"], ["178"])
        self.assertEqual(properties["x-start-latlng"], ["45.0,-122.0"])
        self.assertEqual(properties["x-end-latlng"], ["45.01,-122.01"])
        self.assertEqual(properties["x-kudos-count"], ["7"])

    def test_omits_heartrate_when_has_heartrate_false(self):
        activity = _activity_payload(
            distance=1000,
            has_heartrate=False,
            average_heartrate=150,
            max_heartrate=170,
        )
        mf2 = _build_mf2(activity, track_url="")
        properties = mf2["activity"][0]["properties"]

        self.assertNotIn("x-average-heartrate", properties)
        self.assertNotIn("x-max-heartrate", properties)

    def test_omits_fields_absent_from_activity(self):
        activity = _activity_payload()  # no distance/elevation/etc.
        mf2 = _build_mf2(activity, track_url="")
        properties = mf2["activity"][0]["properties"]

        for key in (
            "x-distance", "x-moving-time", "x-elapsed-time", "x-total-elevation-gain",
            "x-average-speed", "x-max-speed", "x-average-heartrate", "x-max-heartrate",
            "x-start-latlng", "x-end-latlng", "x-kudos-count",
        ):
            self.assertNotIn(key, properties)


class ImportActivityTests(TestCase):
    def setUp(self):
        self.account = _make_account()

    @patch("strava_integration.importer.client.list_activity_photos")
    @patch("strava_integration.importer.client.get_activity_streams")
    @patch("strava_integration.importer.client.get_activity")
    def test_creates_activity_post(self, mock_get_activity, mock_streams, mock_photos):
        mock_get_activity.return_value = _activity_payload()
        mock_streams.return_value = {}
        mock_photos.return_value = []

        post = import_activity(self.account, 1001)

        self.assertIsNotNone(post)
        self.assertEqual(post.kind, Post.ACTIVITY)
        self.assertEqual(post.title, "Morning Run")
        self.assertEqual(post.content, "Felt good.")
        self.assertTrue(StravaActivity.objects.filter(strava_activity_id="1001").exists())

    @patch("strava_integration.importer.client.list_activity_photos")
    @patch("strava_integration.importer.client.get_activity_streams")
    @patch("strava_integration.importer.client.get_activity")
    def test_generates_content_summary_when_description_missing(self, mock_get_activity, mock_streams, mock_photos):
        mock_get_activity.return_value = _activity_payload(
            description="",
            sport_type="Run",
            distance=8368.6,
            moving_time=2531,
            total_elevation_gain=95.1,
        )
        mock_streams.return_value = {}
        mock_photos.return_value = []

        post = import_activity(self.account, 1001)

        self.assertEqual(post.content, "Ran 5.2 mi in 42:11 (8:07/mi pace), 312 ft elevation gain.")

    @patch("strava_integration.importer.client.list_activity_photos")
    @patch("strava_integration.importer.client.get_activity_streams")
    @patch("strava_integration.importer.client.get_activity")
    def test_content_summary_respects_metric_site_setting(self, mock_get_activity, mock_streams, mock_photos):
        config = SiteConfiguration.get_solo()
        config.activity_units = SiteConfiguration.ACTIVITY_UNITS_METRIC
        config.save(update_fields=["activity_units"])

        mock_get_activity.return_value = _activity_payload(
            description="",
            sport_type="Ride",
            distance=20000,
            moving_time=3600,
            total_elevation_gain=150,
        )
        mock_streams.return_value = {}
        mock_photos.return_value = []

        post = import_activity(self.account, 1001)

        self.assertEqual(post.content, "Rode 20.0 km in 1:00:00 (3:00/km pace), 150 m elevation gain.")

    @patch("strava_integration.importer.client.list_activity_photos")
    @patch("strava_integration.importer.client.get_activity_streams")
    @patch("strava_integration.importer.client.get_activity")
    def test_idempotent_second_import_is_a_noop(self, mock_get_activity, mock_streams, mock_photos):
        mock_get_activity.return_value = _activity_payload()
        mock_streams.return_value = {}
        mock_photos.return_value = []

        first = import_activity(self.account, 1001)
        second = import_activity(self.account, 1001)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(StravaActivity.objects.filter(strava_activity_id="1001").count(), 1)
        self.assertEqual(Post.objects.filter(kind=Post.ACTIVITY).count(), 1)

    @patch("strava_integration.importer.client.get_activity")
    def test_private_activity_skipped_when_flagged(self, mock_get_activity):
        mock_get_activity.return_value = _activity_payload(private=True)

        post = import_activity(self.account, 1001, skip_if_private=True)

        self.assertIsNone(post)
        self.assertFalse(StravaActivity.objects.exists())

    @patch("strava_integration.importer.client.list_activity_photos")
    @patch("strava_integration.importer.client.get_activity_streams")
    @patch("strava_integration.importer.client.get_activity")
    def test_private_activity_imported_when_not_skipped(self, mock_get_activity, mock_streams, mock_photos):
        mock_get_activity.return_value = _activity_payload(private=True)
        mock_streams.return_value = {}
        mock_photos.return_value = []

        post = import_activity(self.account, 1001, skip_if_private=False)

        self.assertIsNotNone(post)

    @patch("strava_integration.importer.client.list_activity_photos")
    @patch("strava_integration.importer.client.get_activity_streams")
    @patch("strava_integration.importer.client.get_activity")
    def test_gpx_attached_when_latlng_stream_present(self, mock_get_activity, mock_streams, mock_photos):
        mock_get_activity.return_value = _activity_payload()
        mock_streams.return_value = {
            "latlng": {"data": [[45.0, -122.0], [45.001, -122.001]]},
            "altitude": {"data": [10.0, 11.0]},
            "time": {"data": [0, 5]},
        }
        mock_photos.return_value = []

        post = import_activity(self.account, 1001)

        self.assertIsNotNone(post.gpx_attachment)
        self.assertIn("activity", post.mf2)
        track_values = post.mf2["activity"][0]["properties"]["track"]
        self.assertTrue(track_values[0])


class WebhookEventHandlingTests(TestCase):
    def setUp(self):
        self.account = _make_account(auto_post_enabled=True)

    @patch("strava_integration.tasks.import_activity_task.delay")
    def test_activity_create_event_triggers_import(self, mock_delay):
        handle_strava_webhook_event({
            "object_type": "activity",
            "aspect_type": "create",
            "object_id": 555,
            "owner_id": 42,
        })

        mock_delay.assert_called_once_with("555", skip_if_private=True)

    @patch("strava_integration.tasks.import_activity_task.delay")
    def test_update_event_is_ignored(self, mock_delay):
        handle_strava_webhook_event({
            "object_type": "activity",
            "aspect_type": "update",
            "object_id": 555,
        })

        mock_delay.assert_not_called()

    @patch("strava_integration.tasks.import_activity_task.delay")
    def test_ignored_when_auto_post_disabled(self, mock_delay):
        self.account.auto_post_enabled = False
        self.account.save(update_fields=["auto_post_enabled"])

        handle_strava_webhook_event({
            "object_type": "activity",
            "aspect_type": "create",
            "object_id": 555,
        })

        mock_delay.assert_not_called()


class TokenRefreshTests(TestCase):
    @patch("strava_integration.client._refresh_token")
    def test_refresh_persists_rotated_refresh_token(self, mock_refresh):
        from strava_integration.client import get_valid_access_token

        account = _make_account(expires_at=timezone.now() - timedelta(minutes=1))
        mock_refresh.return_value = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_at": int((timezone.now() + timedelta(hours=6)).timestamp()),
        }

        token = get_valid_access_token(account)

        self.assertEqual(token, "new-access-token")
        account.refresh_from_db()
        self.assertEqual(account.access_token, "new-access-token")
        self.assertEqual(account.refresh_token, "new-refresh-token")


class WebhookHandshakeTests(TestCase):
    def test_validate_echoes_challenge_when_token_matches(self):
        from django.test import Client as TestClient

        _make_account(webhook_verify_token="secret-token")
        response = TestClient().get(
            "/strava/webhook/",
            {"hub.mode": "subscribe", "hub.challenge": "abc123", "hub.verify_token": "secret-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"hub.challenge": "abc123"})

    def test_validate_rejects_mismatched_token(self):
        from django.test import Client as TestClient

        _make_account(webhook_verify_token="secret-token")
        response = TestClient().get(
            "/strava/webhook/",
            {"hub.mode": "subscribe", "hub.challenge": "abc123", "hub.verify_token": "wrong"},
        )
        self.assertEqual(response.status_code, 403)


class AdminPageRenderTests(TestCase):
    """Smoke tests that the admin templates render without error, disconnected and connected."""

    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_settings_page_renders_when_disconnected(self):
        response = self.client.get(reverse("site_admin:strava_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connect Strava")

    def test_settings_page_renders_when_connected(self):
        _make_account(auto_post_enabled=True)
        response = self.client.get(reverse("site_admin:strava_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Disconnect")
        self.assertContains(response, "Auto-post new activities")

    def test_import_page_redirects_when_disconnected(self):
        response = self.client.get(reverse("site_admin:strava_import"))
        self.assertRedirects(response, reverse("site_admin:strava_settings"))

    @patch("strava_integration.client.list_activities")
    def test_import_page_renders_activity_list(self, mock_list_activities):
        _make_account()
        mock_list_activities.return_value = [
            {
                "id": 1001,
                "name": "Morning Run",
                "sport_type": "Run",
                "start_date_local": "2026-01-15T08:00:00Z",
                "distance": 5000,
            }
        ]

        response = self.client.get(reverse("site_admin:strava_import"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Morning Run")
        self.assertContains(response, "Not imported")

    @patch("strava_integration.tasks.import_activity_task.delay")
    def test_import_page_queues_selected_activities(self, mock_delay):
        _make_account()

        response = self.client.post(
            reverse("site_admin:strava_import"), {"activity_id": ["1001", "1002"]}
        )

        # fetch_redirect_response=False: the redirect target's GET branch calls
        # the live Strava API, which is out of scope for this test.
        self.assertRedirects(
            response, reverse("site_admin:strava_import"), fetch_redirect_response=False
        )
        self.assertEqual(mock_delay.call_count, 2)
        mock_delay.assert_any_call("1001", skip_if_private=False)
