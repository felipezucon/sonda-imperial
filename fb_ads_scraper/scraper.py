"""Núcleo do scraper: Playwright + interceptação das respostas GraphQL."""

import json
import os
import re
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

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
TOTAL_RESULTS_RE = re.compile(
    r"^\s*[~≈]?\s*([\d\s.,]+)\s+(?:resultados?|results?)\s*$",
    re.IGNORECASE,
)


def parse_total_results(text):
    """Lê apenas linhas que representam o contador total da interface."""
    for line in (text or "").splitlines():
        match = TOTAL_RESULTS_RE.match(line)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group(1))
        if digits:
            return int(digits)
    return None


def has_meta_empty_state(text):
    normalized = unicodedata.normalize("NFKD", text or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = " ".join(normalized.casefold().split())
    return bool(re.search(
        r"\bnenhum anuncio corresponde aos seus criterios de pesquisa\b",
        normalized,
    ))


@contextmanager
def _exclusive_profile(profile_dir):
    if not profile_dir:
        yield
        return
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    with (profile_dir / ".sonda.lock").open("a+b") as lock_file:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("SONDA_PROFILE_IN_USE") from exc
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class AdLibraryScraper:
    def __init__(self, url, max_results=0, headful=False, timeout=600, console=None,
                 on_progress=None, fail_on_incomplete=False, total_only=False,
                 native_user_agent=False, profile_dir=None, browser_channel=None):
        self.url = url
        self.max_results = max_results
        self.headful = headful
        self.timeout = timeout
        self.console = console or Console()
        self.on_progress = on_progress  # callback(count) para interfaces gráficas
        self.fail_on_incomplete = fail_on_incomplete
        self.total_only = total_only
        self.native_user_agent = native_user_agent
        self.profile_dir = profile_dir
        self.browser_channel = browser_channel
        self.cancel_requested = False   # setar True (de outra thread) interrompe a coleta
        self.raw_ads = {}  # ad_archive_id -> objeto bruto
        self.raw_ads_observed = 0
        self.complete = True
        self.stop_reason = None
        self.collection_duration_seconds = 0
        self.total_results = None
        self.total_results_source = None
        self.empty_state_detected = False

    # ------------------------------------------------------------------
    # Ingestão de payloads
    # ------------------------------------------------------------------

    def _ingest_payload(self, payload):
        added = 0
        for raw in extract_ads_from_payload(payload):
            self.raw_ads_observed += 1
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

    def _read_total_results(self, page, timeout=10_000):
        try:
            text = page.locator("body").inner_text(timeout=timeout)
        except Exception:
            return
        self.total_results = parse_total_results(text)
        if self.total_results is not None:
            self.total_results_source = "META_RESULT_COUNTER"
            return True
        if has_meta_empty_state(text):
            self.raw_ads.clear()
            self.raw_ads_observed = 0
            self.total_results = 0
            self.total_results_source = "META_EMPTY_STATE"
            self.empty_state_detected = True
            return True
        return False

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
        with _exclusive_profile(self.profile_dir), sync_playwright() as p:
            context_options = {"locale": "pt-BR", "viewport": {"width": 1440, "height": 900}}
            if not self.native_user_agent:
                context_options["user_agent"] = USER_AGENT
            if self.profile_dir:
                context = p.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=not self.headful,
                    args=["--disable-blink-features=AutomationControlled"], **context_options,
                    **({"channel": self.browser_channel} if self.browser_channel else {}),
                )
                close_browser = context.close
            else:
                browser = p.chromium.launch(
                    headless=not self.headful,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(**context_options)
                close_browser = browser.close
            page = context.new_page()
            page.on("response", self._on_response)

            self.console.print(f"Abrindo: [dim]{self.url}[/dim]")
            response = page.goto(self.url, wait_until="domcontentloaded", timeout=90_000)

            if response and response.status >= 400:
                close_browser()
                raise RuntimeError(f"META_HTTP_ERROR: HTTP {response.status}")

            if "/login" in page.url or "/checkpoint" in page.url:
                close_browser()
                raise RuntimeError(
                    "O Facebook redirecionou para uma página de login/verificação. "
                    "Tente novamente mais tarde ou execute com --headful para resolver manualmente."
                )

            self._dismiss_cookie_banner(page)
            if self.total_only:
                start = time.monotonic()
                deadline = start + 15
                try:
                    while time.monotonic() < deadline:
                        if self._read_total_results(page, timeout=1000):
                            self.collection_duration_seconds = int(time.monotonic() - start)
                            if self.empty_state_detected:
                                self.console.print("[yellow]Meta empty state detected[/yellow]")
                            self.console.print(f"[green]Total results: {self.total_results} ({self.total_results_source})[/green]")
                            return []
                        page.wait_for_timeout(250)
                    self.collection_duration_seconds = int(time.monotonic() - start)
                    self.console.print("[red]TOTAL_RESULTS_NOT_FOUND[/red]")
                    raise RuntimeError("TOTAL_RESULTS_NOT_FOUND")
                finally:
                    close_browser()
            # Keep the normal load window, but stop it as soon as Meta explicitly
            # reports no matching ads instead of entering the full pagination loop.
            for check in range(16):
                if self._read_total_results(page, timeout=250) and (
                    self.empty_state_detected or self.total_results == 0
                ):
                    self.collection_duration_seconds = (check + 1) // 4
                    self.console.print(
                        "[yellow]Meta empty state detected[/yellow]"
                        if self.empty_state_detected
                        else "[yellow]Meta result counter reports zero[/yellow]"
                    )
                    self.console.print("[green]Total results: 0 ({})[/green]".format(self.total_results_source))
                    close_browser()
                    return []
                page.wait_for_timeout(250)
            self._read_total_results(page)
            self._ingest_initial_html(page)
            self._report_progress()

            stagnant = 0
            start = time.monotonic()
            last_count = len(self.raw_ads)

            while not self._reached_limit():
                if self.cancel_requested:
                    if self.fail_on_incomplete:
                        raise RuntimeError("coleta cancelada antes da conclusão")
                    self.complete = False
                    self.stop_reason = "CANCELLED"
                    self.console.print("\n[yellow]Coleta cancelada; exportando o que foi coletado.[/yellow]")
                    break
                if time.monotonic() - start >= self.timeout:
                    if self.fail_on_incomplete:
                        raise RuntimeError("coleta excedeu o timeout")
                    self.complete = False
                    self.stop_reason = "TIME_LIMIT_REACHED"
                    self.console.print(
                        f"\n[yellow]Tempo limite de {self.timeout}s atingido; "
                        "exportando o que foi coletado.[/yellow]"
                    )
                    break
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception as exc:
                    raise RuntimeError(f"falha durante paginação: {exc}") from exc
                page.wait_for_timeout(1500)

                if len(self.raw_ads) == last_count:
                    stagnant += 1
                    if stagnant >= MAX_STAGNANT_SCROLLS:
                        break  # fim da lista
                else:
                    stagnant = 0
                    last_count = len(self.raw_ads)

            self.console.print()  # encerra a linha de progresso
            close_browser()

        self.collection_duration_seconds = int(time.monotonic() - start)

        ads = [parse_ad(raw) for raw in self.raw_ads.values()]
        if self.max_results:
            ads = ads[: self.max_results]
        return ads
