"""Normalização do JSON bruto do GraphQL da Ad Library para um schema plano."""

from datetime import datetime, timezone

from .scoring import force_score


def dig(obj, *keys, default=None):
    """Navega com segurança por dicts/listas aninhados: dig(d, "a", "b", 0)."""
    cur = obj
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def extract_ads_from_payload(obj, found=None):
    """Varre recursivamente qualquer payload JSON e coleta os objetos de anúncio.

    Um anúncio é identificado por conter as chaves "ad_archive_id" e "snapshot",
    independente de onde estiver na árvore (search_results_connection,
    collated_results, deeplink de anúncio único etc.). Isso torna a extração
    resistente a mudanças na estrutura das respostas do Facebook.
    """
    if found is None:
        found = []
    if isinstance(obj, dict):
        if obj.get("ad_archive_id") and "snapshot" in obj:
            found.append(obj)
        else:
            for value in obj.values():
                extract_ads_from_payload(value, found)
    elif isinstance(obj, list):
        for value in obj:
            extract_ads_from_payload(value, found)
    return found


def _text(value):
    """Campos de texto vêm ora como string, ora como {"text": "..."}."""
    if isinstance(value, dict):
        return value.get("text")
    return value


def _epoch_to_date(value):
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def _spend_text(raw):
    spend = raw.get("spend")
    if isinstance(spend, dict):
        lower = spend.get("lower_bound")
        upper = spend.get("upper_bound")
        if lower and upper:
            return f"{lower}-{upper}"
        return lower or upper
    return spend


def _parse_images(snapshot):
    images = []
    for img in snapshot.get("images") or []:
        if not isinstance(img, dict):
            continue
        images.append({
            "original_url": img.get("original_image_url"),
            "resized_url": img.get("resized_image_url"),
        })
    return images


def _parse_videos(snapshot):
    videos = []
    for vid in snapshot.get("videos") or []:
        if not isinstance(vid, dict):
            continue
        videos.append({
            "video_hd_url": vid.get("video_hd_url"),
            "video_sd_url": vid.get("video_sd_url"),
            "video_preview_image_url": vid.get("video_preview_image_url"),
        })
    return videos


def _parse_cards(snapshot, images, videos):
    """Cards de carrossel: agrega mídia em images/videos e devolve os textos."""
    cards = []
    for card in snapshot.get("cards") or []:
        if not isinstance(card, dict):
            continue
        if card.get("original_image_url") or card.get("resized_image_url"):
            images.append({
                "original_url": card.get("original_image_url"),
                "resized_url": card.get("resized_image_url"),
            })
        if card.get("video_hd_url") or card.get("video_sd_url"):
            videos.append({
                "video_hd_url": card.get("video_hd_url"),
                "video_sd_url": card.get("video_sd_url"),
                "video_preview_image_url": card.get("video_preview_image_url"),
            })
        cards.append({
            "title": card.get("title"),
            "body": _text(card.get("body")),
            "caption": card.get("caption"),
            "link_url": card.get("link_url"),
            "link_description": card.get("link_description"),
            "cta_text": card.get("cta_text"),
            "cta_type": card.get("cta_type"),
        })
    return cards


def parse_ad(raw):
    """Converte um objeto bruto de anúncio no schema plano de saída."""
    snapshot = raw.get("snapshot") or {}

    images = _parse_images(snapshot)
    videos = _parse_videos(snapshot)
    cards = _parse_cards(snapshot, images, videos)

    impressions = (
        dig(raw, "impressions_with_index", "impressions_text")
        or raw.get("impressions_text")
    )

    ad = {
        "ad_archive_id": raw.get("ad_archive_id"),
        "page_name": raw.get("page_name") or snapshot.get("page_name"),
        "page_id": raw.get("page_id") or snapshot.get("page_id"),
        "page_like_count": snapshot.get("page_like_count"),
        "page_categories": snapshot.get("page_categories"),
        "page_profile_uri": snapshot.get("page_profile_uri"),
        "page_profile_picture_url": snapshot.get("page_profile_picture_url"),
        "ad_title": _text(snapshot.get("title")),
        "ad_body": _text(snapshot.get("body")),
        "ad_caption": snapshot.get("caption"),
        "cta_text": snapshot.get("cta_text"),
        "cta_type": snapshot.get("cta_type"),
        "link_url": snapshot.get("link_url"),
        "link_description": _text(snapshot.get("link_description")),
        "images": images,
        "videos": videos,
        "cards": cards,
        "impressions": impressions,
        "spend": _spend_text(raw),
        "reach_estimate": raw.get("reach_estimate"),
        "currency": raw.get("currency") or None,
        "start_date": _epoch_to_date(raw.get("start_date")),
        "end_date": _epoch_to_date(raw.get("end_date")),
        "is_active": raw.get("is_active"),
        "total_active_time": raw.get("total_active_time"),
        "publisher_platform": raw.get("publisher_platform") or raw.get("publisher_platforms"),
        "display_format": snapshot.get("display_format"),
        "targeted_or_reached_countries": raw.get("targeted_or_reached_countries"),
        "collation_count": raw.get("collation_count"),
        "entity_type": raw.get("entity_type"),
    }
    ad.update(force_score(ad))
    return ad
