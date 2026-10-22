import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

BASE_URL = "https://x.cima4p.com"
API_URL = f"{BASE_URL}/wp-json/wp/v2"
OUTPUT_FILE = "data/catalog.json"

session = requests.Session()
session.headers.update({
"User-Agent": "SeriesExplorerCatalog/1.0 (public metadata catalog)"
})

def get_json(url, params=None):
response = session.get(url, params=params, timeout=30)
response.raise_for_status()
return response.json()

def get_image(media_id):
if not media_id:
return ""

try:
    media = get_json(f"{API_URL}/media/{media_id}")
    details = media.get("media_details", {})
    sizes = details.get("sizes", {})

    for size_name in ("medium_large", "large", "medium", "thumbnail"):
        if size_name in sizes:
            return sizes[size_name].get("source_url", "")

    return media.get("source_url", "")
except (requests.RequestException, ValueError):
    return ""

def clean_title(title):
title = re.sub(r"<[^>]+>", "", title or "")
title = re.sub(r"\s+", " ", title).strip()
return title

def extract_series_title(title):
# Remove common episode markers from the end of a post title.
patterns = [
r"\s+الحلقة\s*\d+.$",
r"\s+حلقة\s\d+.$",
r"\s+episode\s\d+.$",
r"\s+ep[\s._-]\d+.$",
r"\s+الحلقه\s\d+.*$",
]

result = title
for pattern in patterns:
    result = re.sub(pattern, "", result, flags=re.IGNORECASE)

return result.strip(" -–—|:")

def extract_episode_number(title):
patterns = [
r"الحلقة\s*(\d+)",
r"حلقة\s*(\d+)",
r"episode\s*(\d+)",
r"\bep[\s._-]*(\d+)",
]

for pattern in patterns:
    match = re.search(pattern, title, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

return None

def fetch_posts():
posts = []
page = 1

while True:
    try:
        response = session.get(
            f"{API_URL}/posts",
            params={
                "per_page": 100,
                "page": page,
                "_fields": "id,link,date,slug,title,featured_media,categories,tags"
            },
            timeout=45
        )

        if response.status_code == 400:
            # WordPress commonly returns 400 when page exceeds the total.
            break

        response.raise_for_status()
        batch = response.json()

        if not isinstance(batch, list) or not batch:
            break

        posts.extend(batch)
        print(f"Fetched page {page}: {len(batch)} posts")

        if len(batch) < 100:
            break

        page += 1
        time.sleep(0.3)

    except requests.RequestException as exc:
        # Do not overwrite the existing catalog after an incomplete fetch.
        raise RuntimeError(
            f"Failed while fetching posts page {page}: {exc}"
        ) from exc

return posts

def fetch_categories():
categories = {}
page = 1

while True:
    response = session.get(
        f"{API_URL}/categories",
        params={
            "per_page": 100,
            "page": page,
            "_fields": "id,name,slug,link,count"
        },
        timeout=45
    )

    if response.status_code == 400:
        break

    response.raise_for_status()
    batch = response.json()

    if not isinstance(batch, list) or not batch:
        break

    for category in batch:
        categories[category["id"]] = category

    if len(batch) < 100:
        break

    page += 1
    time.sleep(0.3)

return categories

def main():
print("Fetching public post metadata...")
posts = fetch_posts()

if not posts:
    raise RuntimeError("No posts returned; existing catalog was not changed.")

print(f"Total posts fetched: {len(posts)}")
categories = fetch_categories()

# Group posts by a category where the category name appears to be a series.
grouped = {}

for post in posts:
    raw_title = clean_title(post.get("title", {}).get("rendered", ""))
    if not raw_title:
        continue

    episode_number = extract_episode_number(raw_title)
    category_ids = post.get("categories", [])
    candidate_categories = [
        categories[cid]
        for cid in category_ids
        if cid in categories
    ]

    series_category = next(
        (
            category for category in candidate_categories
            if category.get("count", 0) >= 2
        ),
        None
    )

    if series_category:
        key = f"category-{series_category['id']}"
        series_title = clean_title(series_category.get("name", ""))
        series_url = series_category.get("link", "")
    else:
        series_title = extract_series_title(raw_title)
        key = f"title-{series_title.casefold()}"
        series_url = ""

    if not series_title:
        continue

    item = grouped.setdefault(key, {
        "title": series_title,
        "originalTitle": "",
        "aliases": [],
        "year": None,
        "genres": [],
        "episodeCount": 0,
        "updated": "",
        "image": "",
        "url": series_url,
        "_episode_numbers": set(),
        "_post_count": 0
    })

    item["_post_count"] += 1

    if raw_title not in item["aliases"]:
        item["aliases"].append(raw_title)

    post_date = post.get("date", "")
    if post_date and post_date > item["updated"]:
        item["updated"] = post_date

    if not item["url"]:
        item["url"] = post.get("link", "")

    if episode_number is not None:
        item["_episode_numbers"].add(episode_number)

    if not item["image"] and post.get("featured_media"):
        item["image"] = get_image(post["featured_media"])

    if not item["year"] and post_date[:4].isdigit():
        item["year"] = int(post_date[:4])

    time.sleep(0.05)

series = []

for item in grouped.values():
    episode_numbers = item.pop("_episode_numbers")
    post_count = item.pop("_post_count")

    # The number of episodes may be incomplete if some posts lack episode numbers.
    item["episodeCount"] = max(
        len(episode_numbers),
        max(episode_numbers, default=0)
    )

    if item["episodeCount"] == 0:
        item["episodeCount"] = post_count

    item["updated"] = item["updated"][:10]

    item["genres"] = ["غير مصنف"]
    item["aliases"] = item["aliases"][:30]
    series.append(item)

series.sort(key=lambda item: item["title"].casefold())

catalog = {
    "updatedAt": datetime.now(timezone.utc).isoformat(),
    "series": series,
    "episodes": []
}

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

temp_file = OUTPUT_FILE + ".tmp"
with open(temp_file, "w", encoding="utf-8") as file:
    json.dump(catalog, file, ensure_ascii=False, indent=2)

os.replace(temp_file, OUTPUT_FILE)

print(f"Catalog updated: {len(series)} grouped entries.")
print(f"Saved to {OUTPUT_FILE}")

if name == "main":
main()
