"""
The Movie Database (TMDB): searching for movies and TV shows, their posters, genres and where to
stream them. Where-to-watch data on TMDB comes from JustWatch, per country.

Everything goes through _get, so tests replace that one function. When TMDB_API_KEY isn't set,
enabled() is False and titles are entered by hand.
"""
import hashlib
import json
import logging
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
TIMEOUT = 10
# TMDB says "tv"; the app says "show".
TMDB_KINDS = {"movie": "movie", "show": "tv"}
APP_KINDS = {"movie": "movie", "tv": "show"}
# Ways to watch that come with a subscription or for free; renting and buying aren't shown.
STREAMING = ("flatrate", "free", "ads")


class TMDBError(Exception):
    pass


def enabled():
    return bool(settings.TMDB_API_KEY)


def _get(path, **params):
    """One API call. The key can be the short API key or the long read access token (a JWT)."""
    key = settings.TMDB_API_KEY
    headers = {"Accept": "application/json"}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    params.setdefault("language", settings.TMDB_LANGUAGE)
    url = f"{API}{path}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    try:
        with urlopen(Request(url, headers=headers), timeout=TIMEOUT) as response:
            return json.load(response)
    except (URLError, OSError, ValueError) as exc:
        logger.warning("TMDB request %s failed: %s", path, exc)
        raise TMDBError("TMDB couldn't be reached. Please try again.") from exc


def _year(date):
    return int(date[:4]) if date and date[:4].isdigit() else None


def _result(item, kind=None):
    """A search result in the app's terms, or None for people and other things that aren't titles."""
    kind = kind or APP_KINDS.get(item.get("media_type"))
    if kind is None:
        return None
    movie = kind == "movie"
    return {
        "tmdb_id": item["id"],
        "kind": kind,
        "title": item.get("title" if movie else "name") or "",
        "original_title": item.get("original_title" if movie else "original_name") or "",
        "year": _year(item.get("release_date" if movie else "first_air_date")),
        "poster": item.get("poster_path") or "",
        "overview": item.get("overview") or "",
        "popularity": item.get("popularity") or 0,
    }


def search(query, kind=None, year=None):
    """Movies and shows matching the query, best first. kind: "movie", "show" or None for both."""
    query = " ".join(query.split())
    if not query:
        return []
    digest = hashlib.sha1(query.lower().encode()).hexdigest()
    cache_key = f"tmdb:search:{settings.TMDB_LANGUAGE}:{kind}:{year}:{digest}"
    found = cache.get(cache_key)
    if found is not None:
        return found
    if kind is None:
        data = _get("/search/multi", query=query, include_adult="false")
    elif kind == "movie":
        data = _get("/search/movie", query=query, include_adult="false", year=year)
    else:
        data = _get("/search/tv", query=query, include_adult="false", first_air_date_year=year)
    found = [r for r in (_result(item, kind) for item in data.get("results", [])) if r and r["title"]]
    cache.set(cache_key, found, 60 * 60)
    return found


def streaming(watch_data, region):
    """The services that stream it in a country, from TMDB's watch/providers: [{"name", "logo"}, ...]."""
    offers = (watch_data or {}).get("results", {}).get(region, {})
    found, seen = [], set()
    for way in STREAMING:
        for offer in sorted(offers.get(way, []), key=lambda o: o.get("display_priority", 999)):
            name = offer.get("provider_name", "")
            if name and name not in seen:
                seen.add(name)
                found.append({"name": name, "logo": offer.get("logo_path") or ""})
    return found


def _seasons(data):
    """A show's seasons (not the specials): [{"number", "name", "year", "episodes", "poster", "overview"}, ...]."""
    return [
        {"number": s["season_number"], "name": s.get("name") or f"Season {s['season_number']}",
         "year": _year(s.get("air_date")), "episodes": s.get("episode_count"), "poster": s.get("poster_path") or "",
         "overview": s.get("overview") or ""}
        for s in data.get("seasons", []) if s.get("season_number")
    ]


def details(kind, tmdb_id, region):
    """Title, year, poster, overview, genres, where to stream it and, for shows, the seasons; in one call."""
    data = _get(f"/{TMDB_KINDS[kind]}/{tmdb_id}", append_to_response="watch/providers")
    result = _result(data, kind)
    result["genres"] = [g["name"] for g in data.get("genres", []) if g.get("name")]
    result["providers"] = streaming(data.get("watch/providers"), region)
    result["seasons"] = _seasons(data) if kind == "show" else []
    return result


def seasons(tmdb_id):
    """A show's seasons, for choosing one."""
    cache_key = f"tmdb:seasons:{settings.TMDB_LANGUAGE}:{tmdb_id}"
    found = cache.get(cache_key)
    if found is None:
        found = _seasons(_get(f"/tv/{tmdb_id}"))
        cache.set(cache_key, found, 24 * 60 * 60)
    return found


def genres(kind):
    """TMDB's genre names for movies or for shows, e.g. ["Action", "Adventure", ...]."""
    cache_key = f"tmdb:genres:{settings.TMDB_LANGUAGE}:{kind}"
    found = cache.get(cache_key)
    if found is None:
        found = sorted(g["name"] for g in _get(f"/genre/{TMDB_KINDS[kind]}/list").get("genres", []) if g.get("name"))
        cache.set(cache_key, found, 7 * 24 * 60 * 60)
    return found


def services(region):
    """The streaming services TMDB knows in a country, most popular first: [{"name", "logo", "id"}, ...]."""
    cache_key = f"tmdb:services:v2:{region}"
    found = cache.get(cache_key)
    if found is not None:
        return found
    ranked = {}
    for kind in TMDB_KINDS.values():
        for p in _get(f"/watch/providers/{kind}", watch_region=region).get("results", []):
            priority = p.get("display_priorities", {}).get(region, p.get("display_priority", 999))
            name = p.get("provider_name", "")
            if name and (name not in ranked or priority < ranked[name][0]):
                ranked[name] = (priority, p.get("logo_path") or "", p.get("provider_id"))
    found = [{"name": name, "logo": logo, "id": provider_id}
             for name, (_, logo, provider_id) in sorted(ranked.items(), key=lambda i: (i[1][0], i[0]))]
    cache.set(cache_key, found, 24 * 60 * 60)
    return found


def similar(kind, tmdb_id):
    """Titles TMDB recommends to people who liked this one."""
    cache_key = f"tmdb:similar:{settings.TMDB_LANGUAGE}:{kind}:{tmdb_id}"
    found = cache.get(cache_key)
    if found is None:
        data = _get(f"/{TMDB_KINDS[kind]}/{tmdb_id}/recommendations")
        found = [r for r in (_result(item, kind) for item in data.get("results", [])) if r and r["title"]]
        cache.set(cache_key, found, 24 * 60 * 60)
    return found


def popular_on(kind, region, provider_id):
    """What's popular on one streaming service in a country, as part of the subscription."""
    cache_key = f"tmdb:popular:{settings.TMDB_LANGUAGE}:{kind}:{region}:{provider_id}"
    found = cache.get(cache_key)
    if found is None:
        data = _get(f"/discover/{TMDB_KINDS[kind]}", watch_region=region, with_watch_providers=provider_id,
                    with_watch_monetization_types="flatrate|free|ads", sort_by="popularity.desc",
                    include_adult="false")
        found = [r for r in (_result(item, kind) for item in data.get("results", [])) if r and r["title"]]
        cache.set(cache_key, found, 24 * 60 * 60)
    return found


def regions():
    """Countries with where-to-watch data: [(code, name), ...] sorted by name."""
    found = cache.get("tmdb:regions")
    if found is None:
        data = _get("/watch/providers/regions", language="en-US").get("results", [])
        found = sorted(((r["iso_3166_1"], r.get("english_name") or r["iso_3166_1"]) for r in data), key=lambda r: r[1])
        cache.set("tmdb:regions", found, 7 * 24 * 60 * 60)
    return found
