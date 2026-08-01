# Scraper da Biblioteca de Anúncios do Facebook

Ferramenta CLI em Python que extrai anúncios da Biblioteca de Anúncios do Facebook (pública, sem login) via Playwright/Chromium com interceptação das respostas GraphQL internas.

## Comandos principais

```bash
# Por palavra-chave (constrói a URL sozinho — jeito preferido)
python -m fb_ads_scraper --keyword "nanoblading" --country BR --max-results 100

# Por URL completa da Ad Library
python -m fb_ads_scraper "https://www.facebook.com/ads/library/?...&q=fitness..." --max-results 100

# Anúncios de uma página específica
python -m fb_ads_scraper "https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL&view_all_page_id=<PAGE_ID>"
```

Saída: `output/fb_ads_<busca>_<timestamp>.json` e `.csv`. Opções: `--format json|csv|both`, `--status active|inactive|all`, `--headful` (depuração), `--timeout SEG`.

## Fluxo de análise de concorrência

Quando o usuário pedir análise de concorrência de um nicho/marca (ou invocar `/concorrencia-meta-ads`), use a skill em `.claude/skills/concorrencia-meta-ads/SKILL.md`: perguntar termo-chave, quantidade e localidade (AskUserQuestion), rodar o scraper local, ler o JSON mais recente em `output/` e gerar relatório (principais anunciantes, anúncios mais antigos ainda ativos = prováveis vencedores, formatos, CTAs, ângulos de copy, plataformas, destinos dos links, oportunidades). **Nunca** usar a skill genérica `competitive-brief`/busca na web para isso — os dados vêm do scraper.

## Aplicação desktop (Sonda Imperial)

Interface gráfica do scraper: `SondaImperial.pyw` (launcher pywebview, janela 1240x645 p/ notebook 1280x672) + `app/server.py` (Flask, porta 8674, thread única de scraping com progresso/cancelamento) + `app/templates|static` (tema Star Wars, amarelo #FFE81F). O servidor agrupa variações do mesmo criativo na amostra (`_variation_groups`: mesma página + texto normalizado; rótulos A, B, C... por tamanho) — selo 🧬 clicável no card filtra o grupo. Atalho na área de trabalho ("Sonda Imperial.lnk" → pythonw) com ícone `assets/sonda.ico`. Preview de dev: `.claude/launch.json` (config "sonda-imperial").

## Arquitetura

- `fb_ads_scraper/cli.py` — argparse; modo URL ou `--keyword`
- `fb_ads_scraper/scraper.py` — Playwright: auto-scroll + interceptação de `/api/graphql/`; anúncios identificados por dicts com `ad_archive_id` + `snapshot` (varredura recursiva, resistente a mudanças de estrutura)
- `fb_ads_scraper/parser.py` — normalização para schema plano
- `fb_ads_scraper/scoring.py` — Índice de Força (0-100): 60% tempo no ar (satura 180d) + 25% variações (`collation_count`, satura 10) + 15% plataformas (satura 4); inativos ×0,7. Campos `force_score`/`force_tier`/`days_running` no JSON/CSV. Tiers: lendario ≥70, forte ≥45, regular ≥20, em teste <20. Em análises, priorizar anúncios lendários/fortes como fonte de insights
- `fb_ads_scraper/exporter.py` — JSON (utf-8) e CSV (utf-8-sig p/ Excel)

## Avisos

- Impressões/gastos/alcance só existem para anúncios de política/temas sociais (limitação do Facebook, não bug).
- Console do Windows é cp1252: não usar emojis/símbolos Unicode em `console.print` do código.
- Se a coleta parar de funcionar, o ponto de ajuste é o filtro de respostas em `scraper.py` (`_on_response`).
