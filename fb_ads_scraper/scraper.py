"""Núcleo do scraper: Playwright + interceptação das respostas GraphQL."""

import json
import re
import time

from playwright.sync_api import sync_playwright
from rich.console import Console

from .parser import extract_ads_from_payload, parse_ad

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Textos possíveis do botão de recusar cookies (pt/en/es)
COOKIE_BUTTON_RE = re.compile(
    r"(recusar cookies opcionais|decline optional cookies|rechazar cookies opcionales"
    r"|permitir todos os cookies|allow all cookies)",
    re.IGNORECASE,
)

# Quantas rolagens consecutivas sem anúncios novos indicam fim da lista
MAX_STAGNANT_SCROLLS = 60


class AdLibraryScraper:
    def __init__(self, url, max_results=0, headful=False, timeout=600, console=None,
                 on_progress=None, fail_on_incomplete=False):
        self.url = url
        self.max_results = max_results
        self.headful = headful
        self.timeout = timeout
        self.console = console or Console()
        self.on_progress = on_progress  # callback(count) para interfaces gráficas
        self.fail_on_incomplete = fail_on_incomplete
        self.cancel_requested = False   # setar True (de outra thread) interrompe a coleta
        self.raw_ads = {}  # ad_archive_id -> objeto bruto

    # ------------------------------------------------------------------
    # Ingestão de payloads
    # ------------------------------------------------------------------

    def _ingest_payload(self, payload):
        added = 0
        for raw in extract_ads_from_payload(payload):
            ad_id = str(raw.get("ad_archive_id"))
            if ad_id not in self.raw_ads:
                self.raw_ads[ad_id] = raw
                added += 1
        return added

    def _ingest_text(self, body):
        """As respostas GraphQL podem trazer vários JSONs concatenados por linha."""
        try:
            self._ingest_payload(json.loads(body))
            return
        except json.JSONDecodeError:
            pass
        for line in body.splitlines():
            line = line.strip()
            if not line or "ad_archive_id" not in line:
                continue
            try:
                self._ingest_payload(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _on_response(self, response):
        if "/api/graphql/" not in response.url:
            return
        try:
            body = response.text()
        except Exception:
            return  # corpo indisponível (redirect, request abortada etc.)
        if "ad_archive_id" not in body:
            return
        before = len(self.raw_ads)
        self._ingest_text(body)
        if len(self.raw_ads) > before:
            self._report_progress()

    def _ingest_initial_html(self, page):
        """Fallback: primeiros resultados embutidos em <script type=application/json>."""
        try:
            scripts = page.eval_on_selector_all(
                'script[type="application/json"]',
                "els => els.map(e => e.textContent)",
            )
        except Exception:
            return
        for content in scripts:
            if content and "ad_archive_id" in content:
                try:
                    self._ingest_payload(json.loads(content))
                except json.JSONDecodeError:
                    continue

    # ------------------------------------------------------------------
    # Navegação
    # ------------------------------------------------------------------

    def _dismiss_cookie_banner(self, page):
        try:
            button = page.get_by_role("button", name=COOKIE_BUTTON_RE).first
            button.click(timeout=4000)
            page.wait_for_timeout(800)
        except Exception:
            pass  # banner não apareceu

    def _report_progress(self):
        count = len(self.raw_ads)
        if self.max_results:
            count = min(count, self.max_results)
        limit = f"/{self.max_results}" if self.max_results else ""
        self.console.print(f"[cyan]Anúncios coletados: {count}{limit}[/cyan]", end="\r")
        if self.on_progress:
            try:
                self.on_progress(count)
            except Exception:
                pass

    def _reached_limit(self):
        return self.max_results and len(self.raw_ads) >= self.max_results

    def run(self):
        """Executa o scraping e retorna a lista de anúncios normalizados."""
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not self.headful,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="pt-BR",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.on("response", self._on_response)

            self.console.print(f"Abrindo: [dim]{self.url}[/dim]")
            page.goto(self.url, wait_until="domcontentloaded", timeout=90_000)

            if "/login" in page.url or "/checkpoint" in page.url:
                browser.close()
                raise RuntimeError(
                    "O Facebook redirecionou para uma página de login/verificação. "
                    "Tente novamente mais tarde ou execute com --headful para resolver manualmente."
                )

            self._dismiss_cookie_banner(page)
            page.wait_for_timeout(4000)
            self._ingest_initial_html(page)
            self._report_progress()

            stagnant = 0
            start = time.time()
            last_count = len(self.raw_ads)

            while not self._reached_limit():
                if self.cancel_requested:
                    if self.fail_on_incomplete:
                        raise RuntimeError("coleta cancelada antes da conclusão")
                    self.console.print("\n[yellow]Coleta cancelada; exportando o que foi coletado.[/yellow]")
                    break
                if time.time() - start > self.timeout:
                    if self.fail_on_incomplete:
                        raise RuntimeError("coleta excedeu o timeout")
                    self.console.print(
                        f"\n[yellow]Tempo limite de {self.timeout}s atingido; "
                        "exportando o que foi coletado.[/yellow]"
                    )
                    break
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception as exc:
                    if self.fail_on_incomplete:
                        raise RuntimeError(f"falha durante paginação: {exc}") from exc
                    break
                page.wait_for_timeout(1500)

                if len(self.raw_ads) == last_count:
                    stagnant += 1
                    if stagnant >= MAX_STAGNANT_SCROLLS:
                        break  # fim da lista
                else:
                    stagnant = 0
                    last_count = len(self.raw_ads)

            self.console.print()  # encerra a linha de progresso
            browser.close()

        ads = [parse_ad(raw) for raw in self.raw_ads.values()]
        if self.max_results:
            ads = ads[: self.max_results]
        return ads
