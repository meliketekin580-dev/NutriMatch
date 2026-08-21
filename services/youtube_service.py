from typing import Any

import requests
import streamlit as st


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


class YouTubeServiceError(RuntimeError):
    """Kullanıcıya güvenli biçimde gösterilebilen YouTube servis hatası."""


def _youtube_key() -> str:
    try:
        return str(st.secrets.get("YOUTUBE_API_KEY", "")).strip()
    except Exception:
        return ""


def _build_query(goal: str, level: str) -> str:
    level_text = f"{level.casefold()} seviye"
    queries = {
        "Kilo Verme": f"{level_text} yağ yakma kardiyo antrenmanı",
        "Dengeli Beslenme": f"{level_text} esneme yoga günlük egzersiz",
        "Kas Yapma": f"{level_text} kas antrenmanı",
    }
    return queries.get(goal, f"{level_text} sağlıklı yaşam egzersizi")


@st.cache_data(ttl=3600, show_spinner=False)
def search_youtube_videos(goal: str, level: str) -> list[dict[str, Any]]:
    """Hedef ve seviyeye göre en fazla üç oynatılabilir Türkçe video getirir."""
    api_key = _youtube_key()
    if not api_key:
        raise YouTubeServiceError(
            "YouTube API anahtarı bulunamadı. YOUTUBE_API_KEY değerini secrets.toml dosyasına eklemelisin."
        )

    try:
        response = requests.get(
            YOUTUBE_SEARCH_URL,
            params={
                "key": api_key,
                "part": "snippet",
                "q": _build_query(goal, level),
                "type": "video",
                "maxResults": 3,
                "videoEmbeddable": "true",
                "safeSearch": "strict",
                "regionCode": "TR",
                "relevanceLanguage": "tr",
                "order": "relevance",
            },
            timeout=8,
        )
    except requests.RequestException as exc:
        raise YouTubeServiceError(
            "YouTube'a şu anda ulaşılamıyor. İnternet bağlantını kontrol edip tekrar deneyebilirsin."
        ) from exc

    if response.status_code in {403, 429}:
        raise YouTubeServiceError(
            "YouTube API kotası dolmuş veya istek sınırına ulaşılmış olabilir. Daha sonra tekrar dene."
        )
    if response.status_code != 200:
        raise YouTubeServiceError("YouTube videoları alınırken geçici bir sorun oluştu.")

    try:
        items = response.json().get("items", [])
    except ValueError as exc:
        raise YouTubeServiceError("YouTube'dan geçerli bir yanıt alınamadı.") from exc

    videos: list[dict[str, Any]] = []
    for item in items:
        video_id = str(item.get("id", {}).get("videoId", "")).strip()
        snippet = item.get("snippet", {})
        if not video_id:
            continue
        thumbnails = snippet.get("thumbnails", {})
        thumbnail = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url", "")
        videos.append(
            {
                "id": video_id,
                "title": str(snippet.get("title", "Video")),
                "channel": str(snippet.get("channelTitle", "YouTube")),
                "thumbnail": str(thumbnail),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )

    if not videos:
        raise YouTubeServiceError("Bu hedef ve seviye için uygun video bulunamadı.")
    return videos[:3]
