"""KYC honesty regression — initiate_kyc / verify_kyc must never fake success.

Deterministic, no network/LLM/Redis. Stubs httpx + the Redis setters and drives
the REAL kyc.py handlers to prove:

  [generate]
  G1  QuickEkyc rejection body ({"status":"error","status_code":500,
      "message":"Invalid Aadhaar Number."}) arrives as HTTP 200 but MUST NOT
      produce "OTP has been sent" — the original bug. The vendor reason is shown.
  G2  Genuine success ({"status":200,...,"data":{"ref_id":...}}) DOES say OTP sent.
  G3  Transport error → friendly line, never leaks the exception text/URL.

  [verify]
  V1  Wrong/expired OTP ({"status":400}) → honest failure, never "successful".
  V2  Backend self-error ({"status":500}) → honest failure (the latent bug:
      old code treated anything != 400 as success).
  V3  Genuine success ({"status":200,"data":{name,gender}}) → success + the
      Aadhaar name/gender are persisted.

Run: python test_kyc_honesty.py   (exit 0 = pass)
"""
import asyncio
import types

import tools.booking.kyc as kyc

_passed = 0


def check(cond, label):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        raise SystemExit(1)


class FakeResp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, responder):
        self._responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        return self._responder(url, json)

    async def get(self, url, **kw):
        return self._responder(url, None)


def install_httpx(responder):
    kyc.httpx = types.SimpleNamespace(AsyncClient=lambda *a, **k: FakeClient(responder))


def install_raising_httpx(exc):
    def _factory(*a, **k):
        class _Raiser:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise exc

            async def get(self, *a, **k):
                raise exc
        return _Raiser()
    kyc.httpx = types.SimpleNamespace(AsyncClient=_factory)


# Neutralise Redis dependencies once.
kyc.get_user_phone = lambda uid: "9000012345"
kyc.set_aadhar_user_name = lambda *a, **k: None
kyc.set_aadhar_gender = lambda *a, **k: None


def run():
    print("[generate]")
    # G1 — QuickEkyc rejection must not claim OTP sent
    install_httpx(lambda u, b: FakeResp(
        {"status": "error", "status_code": 500,
         "message": "Invalid Aadhaar Number.", "data": {}, "request_id": 14441736}))
    out = asyncio.run(kyc.initiate_kyc("u1", "9999 8888 7777"))
    check("OTP has been sent" not in out, "G1 rejection does NOT say 'OTP has been sent'")
    check("Invalid Aadhaar Number" in out, "G1 surfaces the vendor reason")

    # G2 — genuine success says OTP sent
    install_httpx(lambda u, b: FakeResp(
        {"status": 200, "message": "OTP sent successfully", "data": {"ref_id": "r1"}}))
    out = asyncio.run(kyc.initiate_kyc("u1", "1234 1234 1234"))
    check("OTP has been sent" in out, "G2 real success says 'OTP has been sent'")

    # G3 — transport error: friendly, no leak
    install_raising_httpx(RuntimeError("ECONNRESET https://apiv2.rentok.com/secret"))
    out = asyncio.run(kyc.initiate_kyc("u1", "1234 1234 1234"))
    check("couldn't reach" in out.lower(), "G3 transport error is friendly")
    check("secret" not in out and "apiv2" not in out, "G3 leaks no URL/exception text")

    print("[verify]")
    # V1 — wrong OTP
    install_httpx(lambda u, b: FakeResp(
        {"status": 400, "message": "OTP verification failed", "data": {}}))
    out = asyncio.run(kyc.verify_kyc("u1", "000000"))
    check("successful" not in out.lower(), "V1 wrong OTP never says 'successful'")
    check("failed" in out.lower(), "V1 wrong OTP is an honest failure")

    # V2 — backend 500 (the latent bug)
    install_httpx(lambda u, b: FakeResp(
        {"status": 500, "message": "Internal Server Error", "data": {}}))
    out = asyncio.run(kyc.verify_kyc("u1", "000000"))
    check("successful" not in out.lower(), "V2 backend 500 never faked as success")

    # V3 — genuine success persists identity
    captured = {}
    kyc.set_aadhar_user_name = lambda uid, n: captured.__setitem__("name", n)
    kyc.set_aadhar_gender = lambda uid, g: captured.__setitem__("gender", g)

    def _ok(url, body):
        if "verifyAadharOTP" in url:
            return FakeResp({"status": 200, "message": "OTP verified successfully",
                             "data": {"name": "Test User", "gender": "M"}})
        return FakeResp({"status": 200})  # update-kyc
    install_httpx(_ok)
    out = asyncio.run(kyc.verify_kyc("u1", "123456"))
    check("successful" in out.lower(), "V3 real success says 'successful'")
    check(captured.get("name") == "Test User", "V3 persists Aadhaar name")

    print(f"\n{_passed}/{_passed} assertions passed")


if __name__ == "__main__":
    run()
