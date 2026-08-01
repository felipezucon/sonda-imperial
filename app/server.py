"""Sonda Imperial — servidor local da interface do scraper."""

import json
import os
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, render_template, request, send_from_directory

from fb_ads_scraper.exporter import export
from fb_ads_scraper.scoring import force_score
from fb_ads_scraper.scraper import AdLibraryScraper

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
ASSETS_DIR = ROOT / "assets"

app = Flask(__name__)

_lock = threading.Lock()
JOB = {
    "running": False,
    "count": 0,
    "max": 0,
    "label": "",
    "error": None,
    "done": False,
    "files": {},
    "ads": [],
}
_current_scraper = None


def _build_url(keyword, country, status):
    return (
        "https://www.facebook.com/ads/library/"
        f"?active_status={status}&ad_type=all&country={country.upper()}"
        f"&q={quote(keyword)}&search_type=keyword_unordered&media_type=all"
    )


def _run_job(url, max_results, label):
    global _current_scraper
    try:
        def on_progress(count):
            JOB["count"] = min(count, max_results) if max_results else count

        scraper = AdLibraryScraper(url=url, max_results=max_results, on_progress=on_progress)
        _current_scraper = scraper
        ads = scraper.run()
        if ads:
            paths = export(ads, url, output_dir=str(OUTPUT_DIR), fmt="both")
            JOB["files"] = {p.suffix.lstrip("."): p.name for p in paths}
        JOB["ads"] = ads
        JOB["count"] = len(ads)
        if not ads:
            JOB["error"] = "Nenhum anúncio encontrado para essa busca."
    except Exception as exc:  # noqa: BLE001 — erro vai para a interface
        JOB["error"] = str(exc)
    finally:
        JOB["running"] = False
        JOB["done"] = True
        _current_scraper = None


# ---------------------------------------------------------------------------
# Páginas e estáticos
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/scrape")
def api_scrape():
    data = request.get_json(force=True)
    keyword = (data.get("keyword") or "").strip()
    url = (data.get("url") or "").strip()
    country = (data.get("country") or "BR").strip() or "BR"
    status = data.get("status") or "active"
    try:
        max_results = max(0, int(data.get("max_results") or 0))
    except (TypeError, ValueError):
        max_results = 100

    if not url and not keyword:
        return jsonify({"error": "Informe um termo de busca ou uma URL da Biblioteca de Anúncios."}), 400
    if not url:
        url = _build_url(keyword, country, status)
    elif "facebook.com" not in url or "/ads/library" not in url:
        return jsonify({"error": "A URL precisa ser da Biblioteca de Anúncios do Facebook."}), 400

    with _lock:
        if JOB["running"]:
            return jsonify({"error": "Já existe uma varredura em andamento."}), 409
        JOB.update(running=True, count=0, max=max_results, error=None, done=False,
                   files={}, ads=[], label=keyword or "URL personalizada")
        threading.Thread(target=_run_job, args=(url, max_results, keyword), daemon=True).start()

    return jsonify({"ok": True})


@app.post("/api/cancel")
def api_cancel():
    if _current_scraper is not None:
        _current_scraper.cancel_requested = True
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    return jsonify({k: JOB[k] for k in ("running", "count", "max", "label", "error", "done", "files")})


def _norm_text(s):
    """Normaliza texto para comparação: minúsculas, sem pontuação/acentos extras."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:150]


def _group_label(i):
    """0 -> A, 1 -> B ... 25 -> Z, 26 -> AA ..."""
    label = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        label = chr(65 + r) + label
    return label


def _variation_groups(ads):
    """Agrupa anúncios que são variações do mesmo criativo (mesma página + mesmo texto).

    Retorna {ad_archive_id: (rótulo, tamanho_do_grupo)} apenas para grupos com 2+.
    """
    buckets = {}
    for ad in ads:
        text_key = _norm_text(ad.get("ad_body")) or _norm_text(ad.get("ad_title")) \
            or (ad.get("link_url") or "")
        if not text_key:
            continue
        buckets.setdefault((ad.get("page_id"), text_key), []).append(ad)

    groups = sorted((g for g in buckets.values() if len(g) >= 2), key=len, reverse=True)
    var_map = {}
    for i, group in enumerate(groups):
        label = _group_label(i)
        for ad in group:
            var_map[str(ad.get("ad_archive_id"))] = (label, len(group))
    return var_map


def _ensure_score(ad):
    """Arquivos antigos (sem Índice de Força) ganham a pontuação na leitura."""
    if ad.get("force_score") is None:
        ad.update(force_score(ad))
    return ad


def _slim(ad, var_map=None):
    var_info = (var_map or {}).get(str(ad.get("ad_archive_id")))
    thumb = None
    if ad.get("images"):
        thumb = ad["images"][0].get("resized_url") or ad["images"][0].get("original_url")
    is_video = bool(ad.get("videos"))
    if not thumb and is_video:
        thumb = ad["videos"][0].get("video_preview_image_url")
    body = ad.get("ad_body") or ""
    return {
        "id": ad.get("ad_archive_id"),
        "page_name": ad.get("page_name"),
        "page_likes": ad.get("page_like_count"),
        "title": ad.get("ad_title"),
        "body": body[:220] + ("…" if len(body) > 220 else ""),
        "cta": ad.get("cta_text") or ad.get("cta_type"),
        "link_url": ad.get("link_url"),
        "start_date": ad.get("start_date"),
        "is_active": ad.get("is_active"),
        "thumb": thumb,
        "is_video": is_video,
        "format": ad.get("display_format"),
        "score": ad.get("force_score"),
        "tier": ad.get("force_tier"),
        "days": ad.get("days_running"),
        "variations": ad.get("collation_count"),
        "var_group": var_info[0] if var_info else None,
        "var_size": var_info[1] if var_info else None,
    }


def _fmt_bucket(ad):
    if ad.get("cards"):
        return "carrossel"
    if ad.get("videos"):
        return "vídeo"
    if ad.get("images"):
        return "imagem"
    return "outro"


def _aggregate(ads, var_map=None):
    pages = Counter(a.get("page_name") or "?" for a in ads)
    likes = {a.get("page_name"): a.get("page_like_count") for a in ads}
    ctas = Counter((a.get("cta_text") or a.get("cta_type") or "—") for a in ads)
    formats = Counter(_fmt_bucket(a) for a in ads)
    dests = Counter(_dest_bucket(a.get("link_url")) for a in ads)
    scored = [a for a in ads if a.get("force_score") is not None]
    top_force = sorted(scored, key=lambda a: a["force_score"], reverse=True)[:6]
    avg_force = round(sum(a["force_score"] for a in scored) / len(scored)) if scored else None
    return {
        "total": len(ads),
        "advertisers": len(pages),
        "formats": dict(formats.most_common()),
        "top_pages": [{"name": n, "count": c, "likes": likes.get(n)} for n, c in pages.most_common(6)],
        "top_ctas": [{"name": n, "count": c} for n, c in ctas.most_common(6)],
        "destinations": dict(dests.most_common()),
        "top_force": [_slim(a, var_map) for a in top_force],
        "avg_force": avg_force,
        "legendary_count": sum(1 for a in scored if a["force_score"] >= 70),
        "variation_groups": len(set(v[0] for v in (var_map or {}).values())),
        "ads_in_groups": len(var_map or {}),
    }


def _dest_bucket(link):
    if not link:
        return "sem link"
    link = link.lower()
    if "wa.me" in link or "whatsapp" in link:
        return "WhatsApp"
    if "instagram.com" in link:
        return "Instagram"
    if "facebook.com" in link or "fb.me" in link:
        return "Facebook"
    return "site próprio"


@app.get("/api/results")
def api_results():
    fname = request.args.get("file")
    files = dict(JOB["files"])
    if fname:
        safe = os.path.basename(fname)
        path = OUTPUT_DIR / safe
        if not path.exists():
            return jsonify({"error": "Arquivo não encontrado."}), 404
        with open(path, encoding="utf-8") as f:
            ads = json.load(f)
        files = {"json": safe}
        csv_twin = Path(safe).with_suffix(".csv")
        if (OUTPUT_DIR / csv_twin).exists():
            files["csv"] = str(csv_twin)
    else:
        ads = JOB["ads"]
    if not ads:
        return jsonify({"error": "Sem resultados para exibir."}), 404
    for ad in ads:
        _ensure_score(ad)
    var_map = _variation_groups(ads)
    ordered = sorted(ads, key=lambda a: a.get("force_score") or 0, reverse=True)
    return jsonify({
        "summary": _aggregate(ads, var_map),
        "ads": [_slim(a, var_map) for a in ordered[:80]],
        "files": files,
    })


@app.get("/api/history")
def api_history():
    OUTPUT_DIR.mkdir(exist_ok=True)
    items = []
    for p in sorted(OUTPUT_DIR.glob("fb_ads_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        parts = p.stem.split("_")
        items.append({
            "file": p.name,
            "label": "_".join(parts[2:-1]) if len(parts) > 3 else p.stem,
            "when": parts[-1] if parts else "",
            "size_kb": round(p.stat().st_size / 1024),
        })
    return jsonify(items)


@app.post("/api/open")
def api_open():
    data = request.get_json(force=True)
    target = data.get("target")
    if target == "folder":
        OUTPUT_DIR.mkdir(exist_ok=True)
        os.startfile(OUTPUT_DIR)  # noqa: S606 — app local
        return jsonify({"ok": True})
    safe = os.path.basename(target or "")
    path = OUTPUT_DIR / safe
    if not safe or not path.exists():
        return jsonify({"error": "Arquivo não encontrado."}), 404
    os.startfile(path)  # noqa: S606
    return jsonify({"ok": True})


def run(port=8674):
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()
