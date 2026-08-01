"""Índice de Força — pontuação (0-100) de longevidade/investimento de um anúncio.

Premissa: anúncio que fica muito tempo no ar está gerando resultado, senão o
anunciante o pausaria. Sinais usados:

- Tempo no ar (60 pts, satura em 180 dias) — o sinal principal
- Variações do criativo / collation_count (25 pts, satura em 10) — anunciante
  que replica um criativo em vários conjuntos está escalando um vencedor
- Amplitude de plataformas (15 pts, satura em 4) — distribuição ampla indica
  aposta consolidada

Anúncios inativos recebem penalidade de 30%: tiveram vida, mas não sobreviveram.
"""

from datetime import date

WEIGHT_TIME = 60
WEIGHT_VARIATIONS = 25
WEIGHT_PLATFORMS = 15
SATURATION_DAYS = 180
SATURATION_VARIATIONS = 10
SATURATION_PLATFORMS = 4
INACTIVE_PENALTY = 0.7

TIERS = [
    (70, "lendario"),   # veterano escalado: estude e modele
    (45, "forte"),      # sobrevivente comprovado
    (20, "regular"),    # rodando, ainda se provando
    (0, "em teste"),    # recém-lançado: observar, não copiar ainda
]


def days_running(ad, today=None):
    """Dias de veiculação: início até hoje (ativo) ou até a data final (inativo)."""
    start = ad.get("start_date")
    if not start:
        return None
    try:
        start_d = date.fromisoformat(start)
    except ValueError:
        return None
    end_d = today or date.today()
    if not ad.get("is_active") and ad.get("end_date"):
        try:
            end_d = date.fromisoformat(ad["end_date"])
        except ValueError:
            pass
    return max(0, (end_d - start_d).days)


def force_score(ad, today=None):
    """Retorna {"force_score", "force_tier", "days_running"} para um anúncio."""
    days = days_running(ad, today)
    if days is None:
        return {"force_score": None, "force_tier": None, "days_running": None}

    time_pts = min(days / SATURATION_DAYS, 1) * WEIGHT_TIME

    variations = ad.get("collation_count") or 1
    var_pts = min(max(variations - 1, 0) / (SATURATION_VARIATIONS - 1), 1) * WEIGHT_VARIATIONS

    platforms = len(ad.get("publisher_platform") or [])
    plat_pts = min(platforms / SATURATION_PLATFORMS, 1) * WEIGHT_PLATFORMS

    score = time_pts + var_pts + plat_pts
    if not ad.get("is_active"):
        score *= INACTIVE_PENALTY
    score = round(score)

    tier = next(label for threshold, label in TIERS if score >= threshold)
    return {"force_score": score, "force_tier": tier, "days_running": days}
