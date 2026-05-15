"""Google Places API (New) Text Search client。
用同一份 LINE_LEAD_KEYWORDS 撈台灣商家,後續由 line_lead 流程接手抽 LINE。"""
import math

import requests

from config import (
    GOOGLE_PLACES_API_KEY,
    PLACES_LANGUAGE_CODE,
    PLACES_MAX_RESULTS_PER_KEYWORD,
    PLACES_RADIUS_METERS,
    PLACES_REGION_CODE,
)

_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.editorialSummary",
        "places.primaryType",
    ]
)


class PlacesConfigError(Exception):
    pass


def _normalize(p: dict) -> dict:
    """攤平 Places API 的巢狀欄位,讓 caller 拿到單層 dict。"""
    name_obj = p.get("displayName") or {}
    summary_obj = p.get("editorialSummary") or {}
    return {
        "place_id": p.get("id") or "",
        "name": name_obj.get("text") or "",
        "formatted_address": p.get("formattedAddress") or "",
        "website_uri": p.get("websiteUri") or "",
        "maps_uri": p.get("googleMapsUri") or "",
        "editorial_summary": summary_obj.get("text") or "",
        "primary_type": p.get("primaryType") or "",
    }


def search_places(
    keyword: str,
    anchor: dict | None = None,
    page_size: int | None = None,
) -> list[dict]:
    """Text Search 一個關鍵字,回傳 normalize 過的 dict list。
    anchor = {'lat': float, 'lng': float, 'radius_m'?: float} 會以
    locationRestriction.circle 鎖在 anchor 半徑內(預設半徑 PLACES_RADIUS_METERS)。
    quota 失敗 / API key 沒設一律 raise PlacesConfigError 給上層處理。"""
    if not GOOGLE_PLACES_API_KEY:
        raise PlacesConfigError("GOOGLE_PLACES_API_KEY 未設,跳過 Places pipeline")

    body: dict = {
        "textQuery": keyword,
        "languageCode": PLACES_LANGUAGE_CODE,
        "regionCode": PLACES_REGION_CODE,
    }
    size = page_size or PLACES_MAX_RESULTS_PER_KEYWORD
    if size:
        body["pageSize"] = min(max(size, 1), 20)  # API 上限 20

    if anchor:
        # :searchText 的 locationRestriction 只吃 rectangle (circle 只能用在 locationBias),
        # 所以把 center + radius 換算成外接正方形 bbox 作硬鎖。
        lat = float(anchor["lat"])
        lng = float(anchor["lng"])
        radius = min(max(float(anchor.get("radius_m") or PLACES_RADIUS_METERS), 1.0), 50000.0)
        dlat = radius / 111320.0
        dlng = radius / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
        body["locationRestriction"] = {
            "rectangle": {
                "low": {"latitude": lat - dlat, "longitude": lng - dlng},
                "high": {"latitude": lat + dlat, "longitude": lng + dlng},
            }
        }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    r = requests.post(_ENDPOINT, json=body, headers=headers, timeout=15)
    if r.status_code == 403 or r.status_code == 401:
        raise PlacesConfigError(f"Places API 拒絕 ({r.status_code}): {r.text[:200]}")
    if r.status_code >= 400:
        # 把 Google 的 error body 帶進來,debug 不用再 curl
        raise requests.HTTPError(
            f"{r.status_code} {r.reason}: {r.text[:300]}", response=r
        )
    data = r.json() or {}
    return [_normalize(p) for p in data.get("places", [])]
