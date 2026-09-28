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
LOAD_MORE_NO_PROGRESS_LIMIT = 2
LOAD_MORE_WAIT_SECONDS = 8
PAGINATION_OBSERVE_SECONDS = 3
PAGINATION_POLL_MS = 250
PAGINATION_IDLE_FINISH_SECONDS = 15
PAGINATION_BUTTON_CHECK_SECONDS = 1
LOAD_MORE_LABELS = {"ver mais", "ver mais anuncios", "see more", "load more"}
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


def _find_global_load_more(page):
    buttons = page.locator('button,[role="button"]').evaluate_all(r"""
        els => {
          const items = els.map((el, index) => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            const name = (el.getAttribute('aria-label') || el.innerText || el.value || '')
              .normalize('NFKC').replace(/[\u200B\uFEFF]/g, '').normalize('NFD')
              .replace(/[\u0300-\u036f]/g, '').replace(/\s+/g, ' ').trim().toLocaleLowerCase();
            return {
              index, name,
              visible: style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
              enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
              inDialog: !!el.closest('[role="dialog"],dialog'),
              inArticle: !!el.closest('article,[role="article"]'),
              nearBottom: rect.top >= window.innerHeight * 0.5,
              pageAtBottom: window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 8
            };
          });
          const adActions = items.filter(button =>
            button.visible && /^(ver detalhes do anuncio|see ad details|view ad details)$/i.test(button.name)
          );
          const lastAdAction = adActions.length ? els[adActions[adActions.length - 1].index] : null;
          return items.map((item, index) => {
            item.afterLastAd = !!lastAdAction &&
              !!(lastAdAction.compareDocumentPosition(els[index]) & Node.DOCUMENT_POSITION_FOLLOWING);
            return item;
          });
        }
    """)
    matches = [item for item in buttons if item["name"] in LOAD_MORE_LABELS
               and item["visible"] and item["enabled"] and not item["inDialog"]
               and not item["inArticle"] and item["afterLastAd"] and item["nearBottom"]
               and item["pageAtBottom"]]
    if len(matches) != 1:
        return None
    item = matches[0]
    return page.locator('button,[role="button"]').nth(item["index"])


def _pagination_state(page):
    try:
        return page.evaluate("""() => {
          const height = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
          return {height, atBottom: window.scrollY + window.innerHeight >= height - 8};
        }""") or {"height": 0, "atBottom": False}
    except Exception:
        return {"height": 0, "atBottom": False}


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
                 native_user_agent=False, profile_dir=None, browser_channel=None,
                 cdp_endpoint=None):
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
        self.cdp_endpoint = cdp_endpoint
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
            if self.cdp_endpoint:
                try:
                    browser = p.chromium.connect_over_cdp(self.cdp_endpoint, timeout=10_000)
                except Exception as exc:
                    raise RuntimeError(f"SONDA_CDP_CONNECT_FAILED: {exc}") from exc
                if not browser.contexts:
                    raise RuntimeError("SONDA_CDP_CONTEXT_UNAVAILABLE")
                context = browser.contexts[0]
                disconnect_browser = True
            else:
                context_options = {"locale": "pt-BR", "viewport": {"width": 1440, "height": 900}}
                if not self.native_user_agent:
                    context_options["user_agent"] = USER_AGENT
                if self.profile_dir:
                    context = p.chromium.launch_persistent_context(
                        str(self.profile_dir), headless=not self.headful,
                        args=["--disable-blink-features=AutomationControlled"], **context_options,
                        **({"channel": self.browser_channel} if self.browser_channel else {}),
                    )
                    browser = None
                else:
                    browser = p.chromium.launch(
                        headless=not self.headful,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    context = browser.new_context(**context_options)
                disconnect_browser = False
            page = context.new_page()

            def close_browser():
                try:
                    page.close()
                finally:
                    if not disconnect_browser and browser:
                        browser.close()
                    elif not disconnect_browser:
                        context.close()

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
            load_more_no_progress = 0
            scroll_cycles = 0
            start = time.monotonic()
            last_progress_at = start
            last_height_change_at = start
            last_count = len(self.raw_ads)
            last_height = _pagination_state(page)["height"]

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
                scroll_cycles += 1

                # Response events ingest GraphQL ads; the embedded HTML is a one-time bootstrap.
                observe_deadline = time.monotonic() + PAGINATION_OBSERVE_SECONDS
                next_button_check = time.monotonic()
                button = None
                cycle_progress = False
                cycle_start_count, cycle_start_height = last_count, last_height
                while True:
                    now = time.monotonic()
                    height = _pagination_state(page)["height"]
                    count = len(self.raw_ads)
                    ids_grew = count > last_count
                    if height != last_height:
                        last_height_change_at = now
                    if count > last_count or height > last_height:
                        last_count, last_height = count, height
                        last_progress_at = now
                        stagnant = 0
                        load_more_no_progress = 0
                        cycle_progress = True
                        if ids_grew:
                            break
                    elif height < last_height:
                        last_height = height
                    if now >= next_button_check:
                        button = _find_global_load_more(page)
                        next_button_check = now + PAGINATION_BUTTON_CHECK_SECONDS
                    remaining = observe_deadline - now
                    if remaining <= 0:
                        break
                    page.wait_for_timeout(min(PAGINATION_POLL_MS, max(1, int(remaining * 1000))))

                height = _pagination_state(page)["height"]
                count = len(self.raw_ads)
                now = time.monotonic()
                if height != last_height:
                    last_height_change_at = now
                if count > last_count or height > last_height:
                    last_count, last_height = count, height
                    last_progress_at = now
                    stagnant = 0
                    load_more_no_progress = 0
                    cycle_progress = True
                elif height < last_height:
                    last_height = height

                if cycle_progress:
                    self.console.print(
                        f"[pagination] progress unique={cycle_start_count}->{last_count} "
                        f"height={cycle_start_height}->{last_height}"
                    )
                    continue

                if button and load_more_no_progress < LOAD_MORE_NO_PROGRESS_LIMIT:
                    load_more_progress = False
                    self.console.print(f"load more detected: scroll cycle {scroll_cycles}")
                    try:
                        button.click(timeout=3000)
                        self.console.print("load more clicked")
                    except Exception as exc:
                        self.console.print(f"load more click failed: {type(exc).__name__}")
                    deadline = time.monotonic() + LOAD_MORE_WAIT_SECONDS
                    while time.monotonic() < deadline:
                        now = time.monotonic()
                        height = _pagination_state(page)["height"]
                        count = len(self.raw_ads)
                        if height != last_height:
                            last_height_change_at = now
                        if count > last_count or height > last_height:
                            self.console.print(
                                f"[pagination] progress unique={last_count}->{count} "
                                f"height={last_height}->{height}"
                            )
                            last_count, last_height = count, height
                            last_progress_at = now
                            stagnant = 0
                            load_more_no_progress = 0
                            load_more_progress = True
                            break
                        if height < last_height:
                            last_height = height
                        page.wait_for_timeout(PAGINATION_POLL_MS)
                    else:
                        load_more_no_progress += 1
                        self.console.print(
                            f"load more made no progress ({load_more_no_progress}/"
                            f"{LOAD_MORE_NO_PROGRESS_LIMIT})"
                        )
                    if load_more_progress:
                        continue

                state = _pagination_state(page)
                button = _find_global_load_more(page)
                idle_seconds = time.monotonic() - last_progress_at
                load_more_available = bool(button) and load_more_no_progress < LOAD_MORE_NO_PROGRESS_LIMIT
                if scroll_cycles % 5 == 0:
                    self.console.print(
                        f"[pagination] cycle={scroll_cycles} unique={len(self.raw_ads)} "
                        f"height={state['height']} atBottom={state['atBottom']} "
                        f"idle={int(idle_seconds)}s"
                    )
                if (state["atBottom"] and len(self.raw_ads) == last_count
                        and state["height"] == last_height and not load_more_available
                        and time.monotonic() - last_height_change_at >= PAGINATION_IDLE_FINISH_SECONDS
                        and idle_seconds >= PAGINATION_IDLE_FINISH_SECONDS):
                    self.console.print(
                        f"[pagination] finished reason=END_OF_RESULTS unique={len(self.raw_ads)} "
                        f"idle={int(idle_seconds)}s"
                    )
                    break

                stagnant += 1
                if stagnant >= MAX_STAGNANT_SCROLLS:
                    scale_gap = (self.total_results is not None and self.total_results > 0
                                 and self.total_results > max(len(self.raw_ads) * 2,
                                                              len(self.raw_ads) + 10))
                    if scale_gap:
                        self.complete = False
                        self.stop_reason = "PAGINATION_STALLED"
                        if self.fail_on_incomplete:
                            raise RuntimeError(self.stop_reason)
                    break

            self.console.print()  # encerra a linha de progresso
            close_browser()

        self.collection_duration_seconds = int(time.monotonic() - start)

        ads = [parse_ad(raw) for raw in self.raw_ads.values()]
        if self.max_results:
            ads = ads[: self.max_results]
        return ads
