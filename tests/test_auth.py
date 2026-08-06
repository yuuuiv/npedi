from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from auth import (
    AutoLoginError,
    NpediAuthenticator,
    TelegramOtpReader,
    TempMailOtpReader,
    update_env_token,
    update_env_values,
)
from captcha_cnn.model import CnnConfig, label_from_path, normalize_answer
from client import AuthExpired, NpediClient
from config import Config


class FixedSolver:
    def solve(self, image: bytes) -> str:
        assert image == b"jpeg"
        return "7"


class FixedOtp:
    def wait_for_code(self, *, not_before: int) -> str:
        assert not_before > 0
        return "918273"


class AuthTests(unittest.TestCase):
    def test_npedi_captcha_answer_is_strictly_four_alphanumeric_chars(self):
        config = CnnConfig(
            image_height=36,
            image_width=111,
            fixed_length=4,
            labels="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            model_weights=Path("model.weights.h5"),
            labeled_image_dir=Path("labeled"),
        )
        self.assertEqual(normalize_answer(" 99hp\n", config), "99HP")
        self.assertEqual(label_from_path(Path("99HP_challenge-id.jpg"), config), "99HP")
        with self.assertRaises(ValueError):
            normalize_answer("99H", config)
        with self.assertRaises(ValueError):
            normalize_answer("99HPE", config)

    def test_npedi_login_contract(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/getSms"):
                return httpx.Response(200, json={"code": 200, "data": None})
            if request.url.path.endswith("/captchaImage"):
                return httpx.Response(200, json={"code": 200, "data": {
                    "uuid": "challenge-id",
                    "img": base64.b64encode(b"jpeg").decode(),
                }})
            return httpx.Response(200, json={"code": 200, "data": {"token": "new-token"}})

        client = httpx.Client(base_url="https://www.npedi.com/onesite-api", transport=httpx.MockTransport(handler))
        auth = NpediAuthenticator(
            base_url="https://www.npedi.com",
            mobile="13800138000",
            captcha_solver=FixedSolver(),
            otp_reader=FixedOtp(),
            client=client,
        )

        self.assertEqual(auth.login(), "new-token")
        self.assertEqual(requests[0].url.path, "/onesite-api/getSms")
        self.assertEqual(requests[0].url.params["mobile"], "13800138000")
        self.assertEqual(requests[2].url.params["code"], "7")
        self.assertEqual(requests[2].url.params["password"], "918273")
        self.assertEqual(requests[2].url.params["uuid"], "challenge-id")

    def test_telegram_reader_rejects_wrong_chat_and_sender(self):
        updates = {
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"date": 100, "chat": {"id": -1}, "from": {"id": 7}, "text": "验证码 111111"}},
                {"update_id": 2, "message": {"date": 100, "chat": {"id": -2}, "from": {"id": 8}, "text": "验证码 222222"}},
                {"update_id": 3, "message": {"date": 100, "chat": {"id": -2}, "from": {"id": 7}, "text": "验证码 333333"}},
            ],
        }
        client = httpx.Client(
            base_url="https://api.telegram.org/botsecret",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=updates)),
        )
        reader = TelegramOtpReader(
            reader_bot_token="secret",
            chat_id="-2",
            sender_bot_id="7",
            timeout_seconds=1,
            client=client,
        )
        self.assertEqual(reader.wait_for_code(not_before=99), "333333")

    def test_temp_mail_reader_requires_two_matching_forwarded_copies(self):
        payload = {
            "count": 5,
            "results": [
                {
                    "id": "old",
                    "created_at": 90,
                    "sender": "support@neofantasy.online",
                    "to": "tmp@example.test",
                    "text": "【宁波舟山港】您的验证码为：111111",
                },
                {
                    "id": "wrong-recipient",
                    "created_at": 101,
                    "sender": "support@neofantasy.online",
                    "to": "other@example.test",
                    "text": "【宁波舟山港】您的验证码为：222222",
                },
                {
                    "id": "wrong-sender",
                    "created_at": 101,
                    "sender": "attacker@example.test",
                    "to": "tmp@example.test",
                    "text": "【宁波舟山港】您的验证码为：333333",
                },
                {
                    "id": "copy-one",
                    "created_at": 101,
                    "sender": "com.hihonor.mms <support@neofantasy.online>",
                    "to": "tmp@example.test",
                    "text": "【宁波舟山港】您的验证码为：549108，请在5分钟内完成验证。UID：10035",
                },
                {
                    "id": "copy-two",
                    "created_at": 102,
                    "sender": "support@neofantasy.online",
                    "to": "tmp@example.test",
                    "text": "【宁波舟山港】您的验证码为：549108，请勿泄露。",
                },
            ],
        }
        client = httpx.Client(
            base_url="https://mail.example.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        )
        reader = TempMailOtpReader(
            base_url="https://mail.example.test",
            address_jwt="secret-address-jwt-value",
            recipient="tmp@example.test",
            allowed_sender="support@neofantasy.online",
            required_text="宁波舟山港",
            required_copies=2,
            timeout_seconds=1,
            client=client,
        )
        self.assertEqual(reader.wait_for_code(not_before=100), "549108")

    def test_temp_mail_reader_stops_on_rejected_address_jwt(self):
        client = httpx.Client(
            base_url="https://mail.example.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "unauthorized"})),
        )
        reader = TempMailOtpReader(
            base_url="https://mail.example.test",
            address_jwt="expired-address-jwt-value",
            recipient="tmp@example.test",
            allowed_sender="support@neofantasy.online",
            required_text="宁波舟山港",
            client=client,
        )
        with self.assertRaisesRegex(AutoLoginError, "Address JWT"):
            reader.wait_for_code(not_before=100)

    def test_env_token_update_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("BASE_URL=https://example.test\nWeb-Token = old-secret\nPAGE_SIZE=200\n", encoding="utf-8")
            update_env_token(path, "new-secret")
            text = path.read_text(encoding="utf-8")
            self.assertIn("Web-Token =new-secret", text)
            self.assertIn("PAGE_SIZE=200", text)
            self.assertNotIn("old-secret", text)

    def test_env_values_update_is_atomic_and_preserves_unrelated_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("BASE_URL=https://example.test\nAUTO_LOGIN=false\n# keep\n", encoding="utf-8")
            update_env_values(path, {"AUTO_LOGIN": "true", "NPEDI_MOBILE": "13800138000"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("AUTO_LOGIN=true", text)
            self.assertIn("NPEDI_MOBILE=13800138000", text)
            self.assertIn("# keep", text)

    def test_client_refreshes_and_retries_original_request_once(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(401 if calls == 1 else 200, json={"code": 401 if calls == 1 else 200})

        cfg = Config(token="old", auto_login=True, request_delay=(0, 0))
        client = NpediClient(cfg)
        client._http.close()
        client._http = httpx.Client(base_url=cfg.api_base, transport=httpx.MockTransport(handler))

        def refresh() -> None:
            cfg.token = "new"
            client._http.headers["ediAuthorization"] = "Bearer new"

        with patch.object(client, "_refresh_token", side_effect=refresh) as mocked:
            self.assertEqual(client.get_json("/getInfo")["code"], 200)
        self.assertEqual(calls, 2)
        mocked.assert_called_once()
        client.close()

    def test_client_does_not_loop_when_new_token_is_rejected(self):
        cfg = Config(token="old", auto_login=True, request_delay=(0, 0))
        client = NpediClient(cfg)
        client._http.close()
        client._http = httpx.Client(
            base_url=cfg.api_base,
            transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"code": 401})),
        )
        with patch.object(client, "_refresh_token") as mocked:
            with self.assertRaises(AuthExpired):
                client.get_json("/getInfo")
        mocked.assert_called_once()
        client.close()


if __name__ == "__main__":
    unittest.main()
