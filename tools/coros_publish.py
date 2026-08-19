"""
Python port of COROS mobile publish/login flow used by CorosLink.
Provides interactive login, publish_coros_watchface, and create_share_link.

This module uses requests and qrcode. Use interactively (do not commit credentials).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import qrcode

# Constants taken from CorosLink
MOBILE_API_BASE_URLS = {
    "eu": "https://apieu.coros.com/coros",
    "us": "https://api.coros.com/coros",
    "cn": "https://apicn.coros.com/coros",
}
MOBILE_API_HOSTNAMES = {k: v.split("//")[1] for k, v in MOBILE_API_BASE_URLS.items()}
DEFAULT_WATCHFACE_REGION = "us"
MOBILE_APP_KEY = "3475792298363620"
MOBILE_LOGIN_IV = b"weloop3_2015_03#"
MOBILE_VERSION_CODE = "407081000"
MOBILE_APP_VERSION = 1125929972137984
MOBILE_USER_SETTING_SCOPE = "CAEQARgBIAEoATABOAFAAQ=="
API_SUCCESS = "0000"

SETTINGS = {
    "installId": "watchfaces.mobileInstallId",
}

# Simple in-memory session store for the local tool
_in_memory_session = None

# Utility: preserve large numeric IDs by quoting specific properties before JSON parse
_long_number_pattern = re.compile(r'("(?:watchFaceThemeId|watchFaceTemplateId|srcWatchFaceTemplateId|watchfaceId|gearId|userId)"\s*:\s*)(\d{16,})(?=\s*[,}])')

def parse_coros_mobile_json(raw: str):
    lossless_raw = _long_number_pattern.sub(lambda m: f'{m.group(1)}"{m.group(2)}"', raw)
    return json.loads(lossless_raw)

def mobile_api_base_url(region: Optional[str]):
    return MOBILE_API_BASE_URLS.get(region or DEFAULT_WATCHFACE_REGION)

def build_mobile_yf_header(install_id: str, timezone_quarters: int, user_id: Optional[str] = None) -> str:
    safe_user_id = user_id if user_id and user_id.isdigit() else "0"
    prefix = json.dumps({
        "appVersion": MOBILE_APP_VERSION,
        "clientType": 1,
        "language": "en-US",
        "mobileName": install_id,
        "releaseType": 1,
        "systemDisplayId": f"c{hashlib.sha256(install_id.encode()).hexdigest()}",
        "systemVersion": "16",
        "timezone": timezone_quarters
    }, separators=(",", ":"))
    suffix = json.dumps({
        "userSettingScope": MOBILE_USER_SETTING_SCOPE,
        "versionCode": MOBILE_VERSION_CODE
    }, separators=(",", ":"))
    return f"{prefix[:-1]},\"userId\":{safe_user_id},{suffix[1:]}"

def get_mobile_install_id():
    # simple random install id
    return hashlib.sha256(str(time.time()).encode()).hexdigest()[:32]

def mobile_headers(access_token: Optional[str] = None, user_id: Optional[str] = None):
    install_id = get_mobile_install_id()
    timezone_qh = -round(time.timezone / 60 / 15)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "YFHeader": build_mobile_yf_header(install_id, timezone_qh, user_id),
        "app-state": "foreground",
        "request-time": str(int(time.time() * 1000)),
    }
    if access_token:
        headers["accesstoken"] = access_token
    return headers

class MobileRequestError(Exception):
    pass

def mobile_request(endpoint: str, method: str = "POST", body=None, access_token: Optional[str] = None, user_id: Optional[str] = None, base_url: Optional[str] = None, allowed_result_codes=None):
    query = f"?accessToken={access_token}" if access_token else ""
    headers = mobile_headers(access_token, user_id)
    url = (base_url or mobile_api_base_url(None)) + endpoint + query
    resp = requests.request(method, url, headers=headers, data=body)
    if not resp.ok:
        raise MobileRequestError(f"COROS mobile request failed (HTTP {resp.status_code}).")
    raw = resp.text
    try:
        payload = parse_coros_mobile_json(raw)
    except Exception:
        raise MobileRequestError("COROS returned an unreadable mobile API response.")
    result = str(payload.get("result") or payload.get("apiCode") or "")
    if result != API_SUCCESS and (not allowed_result_codes or result not in allowed_result_codes):
        if result == "1019":
            raise MobileRequestError("Your COROS mobile session expired. Sign in again.")
        raise MobileRequestError(payload.get("message") or "COROS rejected the mobile request.")
    return {"data": payload.get("data"), "raw": raw}

# Encryption used by mobile client: obfuscate then AES-128-CBC

def encrypt_mobile_login_field(value: str) -> str:
    key = MOBILE_APP_KEY.encode("utf8")
    clear = value.encode("utf8")
    obfuscated = bytearray(clear)
    for i in range(len(obfuscated)):
        obfuscated[i] ^= key[i % len(key)]
    cipher = AES.new(key[:16], AES.MODE_CBC, MOBILE_LOGIN_IV)
    encrypted = cipher.encrypt(pad(bytes(obfuscated), 16))
    return base64.b64encode(encrypted).decode()

def build_mobile_login_payload(account: str, pwd_hash: str, check_status: int = 1):
    return {
        "account": encrypt_mobile_login_field(account),
        "accountType": 2,
        "appKey": MOBILE_APP_KEY,
        "checkStatus": check_status,
        "clientType": 1,
        "hasHrCalibrated": 0,
        "kbValidity": 0,
        "pwd": encrypt_mobile_login_field(pwd_hash.lower()),
        "region": "",
        "skipValidation": False,
    }

def extract_decimal_property(raw: str, property: str) -> Optional[str]:
    escaped = re.escape(property)
    m = re.search(rf'"{escaped}"\s*:\s*(\d+)', raw)
    return m.group(1) if m else None

def build_create_link_body(background_image_id: int, firmware_type: str, source_template_id: str, template_id: str, name: str) -> str:
    if not re.match(r"^\d+$", template_id) or not re.match(r"^\d+$", source_template_id):
        raise ValueError("Invalid template ids")
    payload = {"type":2,"watchFaceTemplateUserCustom":{"backgroundImageId":background_image_id,"firmwareType":firmware_type,"srcWatchFaceTemplateId":int(source_template_id),"watchFaceTemplateId":int(template_id),"watchFaceTemplateName":name}}
    # serialize directly to ensure numeric literals preserved
    return json.dumps(payload, separators=(",", ":"))

def create_coros_watchface_share_link(session: dict, background_image_id: int, firmware_type: str, source_template_id: str, template_id: str, name: str):
    body = build_create_link_body(background_image_id, firmware_type, source_template_id, template_id, name)
    resp = mobile_request("/watchface/share/createLink", method="POST", body=body, access_token=session.get("accessToken"), user_id=session.get("userId"))
    data = resp["data"]
    url = None
    if data and isinstance(data, dict):
        url = data.get("url")
    if not url or not url.startswith("https://faq.coros.com"):
        raise MobileRequestError("COROS did not return an official watchface share link.")
    # generate QR
    qr = qrcode.make(url)
    img_path = f"coros_share_qr_{int(time.time())}.png"
    qr.save(img_path)
    return {"url": url, "qrPath": img_path}

def publish_coros_watchface(session: dict, archive_path: str, name: str, firmware_type: str, background_image_id: int, language: str = "en-US"):
    # read archive bytes
    with open(archive_path, "rb") as f:
        archive_bytes = f.read()
    save_body = {
        "accessToken": session.get("accessToken"),
        "backgroundImageId": background_image_id,
        "firmwareType": firmware_type,
        "language": language,
        "maxWatchFaceVersion": 5,
        "releaseType": 1,
        "saveOrUpdate": 1,
        "srcWatchFaceTemplateId": "__SRC_TEMPLATE_ID__",
        "version": 2,
        "watchFaceTemplateName": name,
    }
    json_param = json.dumps(save_body).replace('"__SRC_TEMPLATE_ID__"', str(0))
    # The original code substitutes the large id; here caller must ensure replacement if needed
    files = {
        "watchFaceTemplateUserCustomZipFile": ("watchFaceTemplateUserCustomZipFile.dat", archive_bytes, "application/zip")
    }
    data = {"jsonParameter": json_param, "saveOrUpdate": "1"}
    url = mobile_api_base_url(session.get("region")) + "/watchFaceTemplateUserCustom/saveOrUpdateV2"
    headers = mobile_headers(session.get("accessToken"), session.get("userId"))
    resp = requests.post(url, headers=headers, data=data, files=files)
    if not resp.ok:
        raise MobileRequestError(f"Publish failed HTTP {resp.status_code}")
    raw = resp.text
    template_id = extract_decimal_property(raw, "watchFaceTemplateId")
    if not template_id:
        raise MobileRequestError("COROS saved the template but did not return its ID.")
    return create_coros_watchface_share_link(session, background_image_id, firmware_type, str(0), template_id, name)

if __name__ == "__main__":
    print("This module provides functions for publishing; run via the webapp or call functions interactively.")
