from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from config import Config
from scripts.history_backfill_watchdog import (
    BEIJING,
    HistoryBackfillWatchdog,
    TempMailSender,
    WatchSpec,
)


class BodyForbiddenResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    @property
    def text(self):  # pragma: no cover - accessing it is the failure
        raise AssertionError("response body must not be read")

    def json(self):  # pragma: no cover - accessing it is the failure
        raise AssertionError("response JSON must not be read")


class FakeHttpClient:
    def __init__(self, statuses: list[int], before_post=None) -> None:
        self.statuses = list(statuses)
        self.before_post = before_post
        self.calls: list[dict] = []

    def post(self, url: str, *, json: dict, headers: dict):
        if self.before_post:
            self.before_post()
        self.calls.append({"url": url, "json": json, "headers": headers})
        return BodyForbiddenResponse(self.statuses.pop(0))


class HistoryBackfillWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_file = self.root / "wrapper.log"
        self.log_file.write_text("heartbeat\n", encoding="utf-8")
        self.state_file = self.root / "watchdog.json"
        self.cfg = Config(
            temp_mail_base_url="https://mail.example.test",
            temp_mail_address_jwt="fixture-address-jwt-secret",
            temp_mail_site_password="fixture-site-secret",
            db_path=self.root / "unused.sqlite",
        )
        self.spec = WatchSpec(
            pid=4242,
            eta_start="2023-01-01",
            eta_end="2026-06-30",
            catalog_scope="all",
            log_file=self.log_file,
            state_file=self.state_file,
            recipient="watchdog-alerts@example.test",
            stale_seconds=900,
            poll_seconds=1,
        )
        self.now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=BEIJING)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_watchdog(
        self,
        http: FakeHttpClient,
        *,
        remaining: int,
        alive: bool,
        heartbeat=None,
    ) -> HistoryBackfillWatchdog:
        return HistoryBackfillWatchdog(
            self.cfg,
            self.spec,
            sender=TempMailSender(self.cfg, self.spec.recipient, client=http),
            remaining_reader=lambda: remaining,
            process_checker=lambda _pid: alive,
            heartbeat_reader=heartbeat or (lambda _path: self.now),
            retry_seconds=60,
        )

    def test_process_exit_persists_event_before_one_send(self) -> None:
        def assert_pending_is_durable() -> None:
            stored = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["event"]["delivery_status"], "pending")
            self.assertEqual(stored["event"]["detected_at"], "2026-08-13T20:00:00+08:00")

        http = FakeHttpClient([200], before_post=assert_pending_is_durable)
        watchdog = self.make_watchdog(http, remaining=271_000, alive=False)

        self.assertEqual(watchdog.tick(self.now), "alert_sent")
        self.assertEqual(len(http.calls), 1)
        call = http.calls[0]
        self.assertEqual(call["url"], "https://mail.example.test/api/send_mail")
        self.assertEqual(call["json"]["to_mail"], self.spec.recipient)
        self.assertEqual(call["json"]["is_html"], False)
        stored = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["event"]["reason"], "process_exited")
        self.assertEqual(stored["event"]["delivery_status"], "sent")
        serialized = json.dumps(stored)
        self.assertNotIn("fixture-address-jwt-secret", serialized)
        self.assertNotIn("fixture-site-secret", serialized)

    def test_sent_event_is_idempotent_across_restart(self) -> None:
        first_http = FakeHttpClient([200])
        first = self.make_watchdog(first_http, remaining=12, alive=False)
        self.assertEqual(first.tick(self.now), "alert_sent")

        second_http = FakeHttpClient([200])
        second = self.make_watchdog(second_http, remaining=12, alive=False)
        self.assertEqual(second.tick(self.now + timedelta(minutes=5)), "alert_sent")
        self.assertEqual(len(first_http.calls), 1)
        self.assertEqual(len(second_http.calls), 0)

    def test_failed_send_stays_pending_and_retries_after_interval(self) -> None:
        http = FakeHttpClient([503, 202])
        watchdog = self.make_watchdog(http, remaining=9, alive=False)

        self.assertEqual(watchdog.tick(self.now), "alert_pending")
        after_failure = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(after_failure["event"]["delivery_status"], "pending")
        self.assertEqual(after_failure["event"]["last_error"], "http_status_503")
        self.assertEqual(watchdog.tick(self.now + timedelta(seconds=30)), "alert_pending")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(watchdog.tick(self.now + timedelta(seconds=61)), "alert_sent")
        self.assertEqual(len(http.calls), 2)

    def test_remaining_zero_is_normal_and_sends_nothing(self) -> None:
        http = FakeHttpClient([200])
        watchdog = self.make_watchdog(http, remaining=0, alive=False)

        self.assertEqual(watchdog.tick(self.now), "completed")
        self.assertEqual(http.calls, [])
        stored = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIsNone(stored["event"])
        self.assertEqual(stored["last_remaining"], 0)

    def test_stale_log_alerts_even_while_process_is_alive(self) -> None:
        stale = self.now - timedelta(minutes=16)
        http = FakeHttpClient([200])
        watchdog = self.make_watchdog(
            http,
            remaining=27,
            alive=True,
            heartbeat=lambda _path: stale,
        )

        self.assertEqual(watchdog.tick(self.now), "alert_sent")
        stored = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["event"]["reason"], "heartbeat_stale")
        self.assertEqual(
            stored["event"]["threshold_crossed_at"],
            "2026-08-13T19:59:00+08:00",
        )

    def test_fresh_log_and_live_process_keep_watching(self) -> None:
        http = FakeHttpClient([200])
        watchdog = self.make_watchdog(http, remaining=27, alive=True)

        self.assertEqual(watchdog.tick(self.now), "watching")
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
