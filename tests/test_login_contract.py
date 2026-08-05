from __future__ import annotations

import json
import unittest

from scripts.extract_login_contract import extract_contract


class LoginContractTests(unittest.TestCase):
    def test_extracts_field_names_without_secret_values(self):
        secrets = {
            "phone": "13800138000",
            "captcha": "a8K2",
            "otp": "918273",
            "token": "secret.jwt.value",
        }
        har = {
            "log": {
                "entries": [{
                    "request": {
                        "method": "POST",
                        "url": f"https://www.npedi.com/onesite-api/login?phone={secrets['phone']}",
                        "headers": [{"name": "Cookie", "value": f"Web-Token={secrets['token']}"}],
                        "cookies": [{"name": "Web-Token", "value": secrets["token"]}],
                        "postData": {
                            "mimeType": "application/json",
                            "text": json.dumps({
                                "mobile": secrets["phone"],
                                "captcha": secrets["captcha"],
                                "smsCode": secrets["otp"],
                                "loginType": "DX",
                            }),
                        },
                    },
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Set-Cookie", "value": f"edi-token={secrets['token']}"}],
                        "cookies": [{"name": "edi-token", "value": secrets["token"]}],
                        "content": {
                            "mimeType": "application/json",
                            "text": json.dumps({"code": 200, "data": {"token": secrets["token"]}}),
                        },
                    },
                }],
            },
        }

        result = extract_contract(har, host="www.npedi.com")
        rendered = json.dumps(result, ensure_ascii=False)

        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["query_fields"], ["phone"])
        self.assertIn("smsCode", result["entries"][0]["request_body_fields"])
        self.assertIn("data.token", result["entries"][0]["response_body_fields"])
        for secret in secrets.values():
            self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
