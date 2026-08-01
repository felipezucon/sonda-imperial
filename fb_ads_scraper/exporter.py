"""Exportação dos resultados em JSON e CSV."""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CSV_COLUMNS = [
    "ad_archive_id", "force_score", "force_tier", "days_running",
    "page_name", "page_id", "page_like_count", "page_categories",
    "page_profile_uri", "page_profile_picture_url",
    "ad_title", "ad_body", "ad_caption",
    "cta_text", "cta_type", "link_url", "link_description",
    "image_original_urls", "image_resized_urls",
    "video_urls", "video_preview_urls",
    "cards_count",
    "impressions", "spend", "reach_estimate", "currency",
    "start_date", "end_date", "is_active", "total_active_time",
    "publisher_platform", "display_format", "targeted_or_reached_countries",
    "collation_count", "entity_type",
]


def slug_from_url(url):
    """Gera um identificador curto a partir da busca (q= ou view_all_page_id=)."""
    params = parse_qs(urlparse(url).query)
    term = (params.get("q") or params.get("view_all_page_id") or ["ads"])[0]
    slug = re.sub(r"[^\w-]+", "-", term, flags=re.UNICODE).strip("-").lower()
    return slug[:40] or "ads"


def _join(values, sep=" | "):
    return sep.join(str(v) for v in values if v) if values else ""


def _csv_row(ad):
    images = ad.get("images") or []
    videos = ad.get("videos") or []
    row = dict(ad)
    row["image_original_urls"] = _join([i.get("original_url") for i in images])
    row["image_resized_urls"] = _join([i.get("resized_url") for i in images])
    row["video_urls"] = _join([v.get("video_hd_url") or v.get("video_sd_url") for v in videos])
    row["video_preview_urls"] = _join([v.get("video_preview_image_url") for v in videos])
    row["cards_count"] = len(ad.get("cards") or [])
    for key in ("page_categories", "publisher_platform", "targeted_or_reached_countries"):
        if isinstance(row.get(key), list):
            row[key] = ", ".join(str(v) for v in row[key])
    return {col: row.get(col, "") for col in CSV_COLUMNS}


def export(ads, url, output_dir="output", fmt="both"):
    """Grava os arquivos e retorna a lista de caminhos criados."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = out / f"fb_ads_{slug_from_url(url)}_{stamp}"
    paths = []

    if fmt in ("json", "both"):
        json_path = base.with_suffix(".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(ads, f, ensure_ascii=False, indent=2)
        paths.append(json_path)

    if fmt in ("csv", "both"):
        csv_path = base.with_suffix(".csv")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for ad in ads:
                writer.writerow(_csv_row(ad))
        paths.append(csv_path)

    return paths
