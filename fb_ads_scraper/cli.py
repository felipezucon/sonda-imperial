"""Interface de linha de comando."""

import argparse
import sys
from urllib.parse import quote, urlparse

from rich.console import Console

from .exporter import export
from .scraper import AdLibraryScraper


def _validate_url(url):
    parsed = urlparse(url)
    host_ok = parsed.netloc.endswith("facebook.com")
    path_ok = "/ads/library" in parsed.path
    return parsed.scheme in ("http", "https") and host_ok and path_ok


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fb_ads_scraper",
        description="Extrai anúncios da Biblioteca de Anúncios do Facebook a partir de uma URL de busca.",
    )
    parser.add_argument("url", nargs="?", default=None,
                        help="URL completa da Biblioteca de Anúncios (facebook.com/ads/library/?...)")
    parser.add_argument("--keyword", help="Palavra-chave para busca (alternativa a informar a URL)")
    parser.add_argument("--country", default="BR",
                        help="País da busca no modo --keyword (padrão: BR; use ALL para todos)")
    parser.add_argument("--status", choices=["active", "inactive", "all"], default="active",
                        help="Status dos anúncios no modo --keyword (padrão: active)")
    parser.add_argument("--max-results", type=int, default=0,
                        help="Limite de anúncios (0 = ilimitado, padrão)")
    parser.add_argument("--format", choices=["json", "csv", "both"], default="both",
                        help="Formato de saída (padrão: both)")
    parser.add_argument("--output", default="output", help="Pasta de saída (padrão: output/)")
    parser.add_argument("--headful", action="store_true",
                        help="Abre o navegador visível (útil para depuração)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Tempo máximo de coleta em segundos (padrão: 600)")
    parser.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--require-complete", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    console = Console()

    if not args.url and args.keyword:
        args.url = (
            "https://www.facebook.com/ads/library/"
            f"?active_status={args.status}&ad_type=all&country={args.country.upper()}"
            f"&q={quote(args.keyword)}&search_type=keyword_unordered&media_type=all"
        )

    if not args.url:
        console.print("[red]Informe uma URL da Biblioteca de Anúncios ou use --keyword.[/red] Ex.:")
        console.print('  python -m fb_ads_scraper --keyword "nanoblading" --country BR --max-results 100')
        return 2

    if not _validate_url(args.url):
        console.print("[red]URL inválida.[/red] Use uma URL da Biblioteca de Anúncios, ex.:")
        console.print("  https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=BR&q=fitness")
        return 2

    scraper = AdLibraryScraper(
        url=args.url,
        max_results=args.max_results,
        headful=args.headful,
        timeout=args.timeout,
        console=console,
        fail_on_incomplete=args.require_complete,
    )

    try:
        ads = scraper.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido pelo usuário; exportando o que foi coletado.[/yellow]")
        from .parser import parse_ad
        ads = [parse_ad(raw) for raw in scraper.raw_ads.values()]
        if args.max_results:
            ads = ads[: args.max_results]
    except RuntimeError as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        return 1

    if not ads and not args.allow_empty:
        console.print(
            "[yellow]Nenhum anúncio encontrado.[/yellow] Verifique se a busca retorna "
            "resultados no navegador ou tente novamente com --headful."
        )
        return 1

    paths = export(ads, args.url, output_dir=args.output, fmt=args.format)
    console.print(f"[green]{len(ads)} anúncios exportados:[/green]")
    for path in paths:
        console.print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
