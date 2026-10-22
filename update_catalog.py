import json
import os
import re
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://x.cima4p.com"
API_URL = f"{BASE_URL}/wp-json/wp/v2"
OUTPUT_FILE = "data/catalog.json"

session = requests.Session()
session.headers.update({
    "User-Agent": "SeriesExplorer/1.0"
})


def get_json(url, params=None):
    response = session.get(
        url,
        params=params,
        timeout=40
    )
    response.raise_for_status()
    return response.json()


def fetch_all(endpoint, fields):
    results = []
    page = 1

    while True:
        try:
            response = session.get(
                f"{API_URL}/{endpoint}",
                params={
                    "per_page": 100,
                    "page": page,
                    "_fields": fields
                },
                timeout=45
            )

            if response.status_code == 400 and page > 1:
                break

            response.raise_for_status()
            batch = response.json()

            if not isinstance(batch, list) or not batch:
                break

            results.extend(batch)
            print(
                f"{endpoint}: page {page}, "
                f"{len(batch)} records"
            )

            total_pages = int(
                response.headers.get("X-WP-TotalPages", "0")
            )

            if total_pages and page >= total_pages:
                break

            if len(batch) < 100:
                break

            page += 1
            time.sleep(0.2)

        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not fetch {endpoint}, page {page}: {exc}"
            ) from exc

    return results


def clean_text(value):
    value = re.sub(r"<[^>]*>", "", value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def episode_number(title):
    patterns = [
        r"الحلقة\s*(\d+)",
        r"الحلقه\s*(\d+)",
        r"حلقة\s*(\d+)",
        r"episode[\s._-]*(\d+)",
        r"\bep[\s._-]*(\d+)"
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            title,
            flags=re.IGNORECASE
        )

        if match:
            return int(match.group(1))

    return None


def base_title(title):
    patterns = [
        r"\s+الحلقة\s*\d+.*$",
        r"\s+الحلقه\s*\d+.*$",
        r"\s+حلقة\s*\d+.*$",
        r"\s+episode[\s._-]*\d+.*$",
        r"\s+ep[\s._-]*\d+.*$"
    ]

    for pattern in patterns:
        title = re.sub(
            pattern,
            "",
            title,
            flags=re.IGNORECASE
        )

    return title.strip(" -–—|:")


def fetch_image(media_id, cache):
    if not media_id:
        return ""

    if media_id in cache:
        return cache[media_id]

    try:
        media = get_json(
            f"{API_URL}/media/{media_id}",
            {"_fields": "source_url,media_details"}
        )

        details = media.get("media_details", {})
        sizes = details.get("sizes", {})

        image_url = ""

        for size in ("medium_large", "large", "medium"):
            if size in sizes:
                image_url = sizes[size].get(
                    "source_url", ""
                )

                if image_url:
                    break

        if not image_url:
            image_url = media.get("source_url", "")

        cache[media_id] = image_url
        return image_url

    except (requests.RequestException, ValueError) as exc:
        print(f"Image unavailable for media {media_id}: {exc}")
        cache[media_id] = ""
        return ""


def main():
    print("Fetching categories...")

    category_list = fetch_all(
        "categories",
        "id,name,slug,link,count"
    )

    categories = {
        item["id"]: item
        for item in category_list
    }

    print(f"Categories fetched: {len(categories)}")
    print("Fetching posts...")

    posts = fetch_all(
        "posts",
        "id,link,date,slug,title,featured_media,categories"
    )

    if not posts:
        raise RuntimeError(
            "No posts were returned. Existing catalog preserved."
        )

    print(f"Posts fetched: {len(posts)}")

    grouped = {}
    image_cache = {}

    for post in posts:
        title = clean_text(
            post.get("title", {}).get("rendered", "")
        )

        if not title:
            continue

        number = episode_number(title)
        category_ids = post.get("categories", [])

        # Prefer a category containing multiple posts.
        category = next(
            (
                categories[cid]
                for cid in category_ids
                if cid in categories
                and categories[cid].get("count", 0) >= 2
            ),
            None
        )

        if category:
            key = f"category-{category['id']}"
            series_title = clean_text(category.get("name", ""))
            series_url = category.get("link", "")
        else:
            series_title = base_title(title)
            key = f"title-{series_title.casefold()}"
            series_url = post.get("link", "")

        if not series_title:
            continue

        if key not in grouped:
            media_id = post.get("featured_media", 0)

            grouped[key] = {
                "title": series_title,
                "originalTitle": "",
                "aliases": [],
                "year": None,
                "genres": ["غير مصنف"],
                "episodeCount": 0,
                "updated": "",
                "image": fetch_image(media_id, image_cache),
                "url": series_url or post.get("link", ""),
                "_numbers": set(),
                "_post_count": 0
            }

        item = grouped[key]
        item["_post_count"] += 1

        if title not in item["aliases"]:
            item["aliases"].append(title)

        if number is not None:
            item["_numbers"].add(number)

        post_date = post.get("date", "")

        if post_date > item["updated"]:
            item["updated"] = post_date

        if not item["year"] and post_date[:4].isdigit():
            item["year"] = int(post_date[:4])

    series = []

    for item in grouped.values():
        numbers = item.pop("_numbers")
        post_count = item.pop("_post_count")

        if numbers:
            item["episodeCount"] = max(numbers)
        else:
            item["episodeCount"] = post_count

        item["updated"] = item["updated"][:10]
        item["aliases"] = item["aliases"][:30]

        series.append(item)

    series.sort(
        key=lambda item: item["title"].casefold()
    )

    catalog = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "series": series,
        "episodes": []
    }

    os.makedirs("data", exist_ok=True)

    temporary_file = OUTPUT_FILE + ".tmp"

    with open(
        temporary_file,
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            catalog,
            file,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temporary_file, OUTPUT_FILE)

    print(f"Catalog saved successfully: {len(series)} entries.")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
