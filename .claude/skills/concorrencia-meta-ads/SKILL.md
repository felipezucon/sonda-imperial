---
name: concorrencia-meta-ads
description: Análise de concorrência na Biblioteca de Anúncios do Facebook/Meta usando o scraper local deste projeto. Use quando o usuário pedir análise de concorrência de anúncios, espionagem de criativos, pesquisa de nicho no Meta Ads, ou invocar /concorrencia-meta-ads. Pergunta termo-chave, quantidade de resultados e localidade, executa o scraper e entrega relatório + arquivos JSON/CSV. NÃO usar a skill genérica competitive-brief para isso.
argument-hint: "<termo-chave opcional>"
---

# Análise de concorrência — Meta Ads (Biblioteca de Anúncios)

Este projeto tem um scraper CLI próprio (`fb_ads_scraper/`, Python + Playwright) que extrai anúncios reais da Biblioteca de Anúncios do Facebook, sem login. Esta skill executa o scraper e gera um relatório de inteligência competitiva. **Não use busca na web nem a skill `competitive-brief` — os dados vêm do scraper local.**

## Passo 1 — Coletar os parâmetros

Argumentos recebidos: **$ARGUMENTS**

Use a ferramenta AskUserQuestion para perguntar (em uma única chamada) o que ainda não foi informado:

1. **Termo-chave** — o nicho/palavra-chave a pesquisar (ex.: "nanoblading", "harmonização facial"). Se já veio nos argumentos, não pergunte.
2. **Quantidade de resultados** — opções: 50 (rápido), 100 (recomendado), 300 (profundo), Ilimitado (até o fim da lista; pode demorar).
3. **Localidade** — opções: BR (Brasil, recomendado), US (Estados Unidos), ALL (todos os países), ou outro código de país ISO-2 informado pelo usuário.

Assuma status `active` (anúncios rodando agora). Só inclua inativos se o usuário pedir.

## Passo 2 — Executar o scraper

```
python -m fb_ads_scraper --keyword "<termo>" --country <PAIS> --max-results <N>
```

- `Ilimitado` → `--max-results 0`
- O comando gera `output/fb_ads_<termo>_<timestamp>.json` e `.csv`
- Se der erro de login/bloqueio, aguarde e tente 1x de novo; persiste → informe o usuário e sugira `--headful`

## Passo 3 — Analisar o JSON gerado

Leia o arquivo JSON mais recente em `output/` e produza um relatório em português com estas seções:

1. **Resumo executivo** — tamanho da amostra, quantos anunciantes distintos, principal achado
2. **Principais anunciantes** — páginas com mais anúncios ativos (volume ≈ investimento; incluir `page_like_count`)
3. **Anúncios "vencedores"** — ordenar por `force_score` (Índice de Força 0-100 já presente no JSON: 60% tempo no ar + 25% variações do criativo + 15% plataformas; `force_tier` lendario/forte/regular/em teste). Anúncios `lendario` (70+) devem ter **peso máximo** nos insights: cite página, dias no ar (`days_running`), variações (`collation_count`) e trecho do texto. Padrões de copy/formato devem ser extraídos prioritariamente dos tiers lendario+forte, usando os "em teste" apenas como tendências emergentes
4. **Formatos dominantes** — proporção vídeo × imagem × carrossel (`display_format`, `videos`, `cards`)
5. **CTAs mais usados** — distribuição de `cta_type`/`cta_text`
6. **Ângulos de copy recorrentes** — padrões nos `ad_body`: dor, promessa, prova social, oferta, urgência, preço; citar exemplos reais
7. **Destinos dos links** — `link_url`: site próprio, WhatsApp, Instagram, página de captura
8. **Plataformas** — distribuição de `publisher_platform`
9. **Oportunidades** — ângulos, formatos e segmentos pouco explorados que o usuário pode aproveitar

## Passo 4 — Entregar

Ao final, liste os caminhos dos arquivos gerados (JSON e CSV) como links clicáveis e pergunte se o usuário quer: baixar os criativos (imagens/vídeos), comparar com outro nicho, ou agendar monitoramento recorrente.

## Observações

- Impressões/gastos/alcance vêm vazios para anúncios comerciais — o Facebook só divulga essas métricas para anúncios de política/temas sociais. Não é erro; não tratar como falha.
- Console do Windows é cp1252: não usar emojis em `console.print`.
