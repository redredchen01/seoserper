"""SERP parser for PAA (People Also Ask) and Related Searches."""

from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from seoserper.models import (
    FailureCategory,
    PAAQuestion,
    ParseResult,
    RelatedSearch,
    SurfaceName,
    SurfaceStatus,
)


@dataclass
class ParserConfig:
    """Configuration for SERP parsing."""

    min_paa_count: int = 1
    min_related_count: int = 1


def parse_serp(
    html: str, locale: str = "en-us", config: Optional[ParserConfig] = None
) -> dict[SurfaceName, ParseResult]:
    """Parse PAA and Related Searches from Google SERP HTML."""
    if config is None:
        config = ParserConfig()

    if not html or not html.strip():
        return {
            SurfaceName.PAA: ParseResult(
                status=SurfaceStatus.FAILED,
                items=[],
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            ),
            SurfaceName.RELATED: ParseResult(
                status=SurfaceStatus.FAILED,
                items=[],
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            ),
        }

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return {
            SurfaceName.PAA: ParseResult(
                status=SurfaceStatus.FAILED,
                items=[],
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            ),
            SurfaceName.RELATED: ParseResult(
                status=SurfaceStatus.FAILED,
                items=[],
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            ),
        }

    paa_result = _parse_paa(soup, config)
    related_result = _parse_related(soup, config)

    return {
        SurfaceName.PAA: paa_result,
        SurfaceName.RELATED: related_result,
    }


def _parse_paa(
    soup: BeautifulSoup, config: ParserConfig
) -> ParseResult:
    """Extract People Also Ask questions from SERP."""
    containers = soup.select(".related-question-pair")

    if not containers:
        return ParseResult(
            status=SurfaceStatus.FAILED,
            items=[],
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
        )

    questions = []
    for rank, container in enumerate(containers, 1):
        question_elem = container.select_one("div[role='heading'] span")
        if not question_elem:
            question_elem = container.select_one(".q")

        if not question_elem:
            continue

        question_text = question_elem.get_text(strip=True)
        if not question_text:
            continue

        answer_elem = container.select_one(".hgKElf")
        answer_preview = (
            answer_elem.get_text(strip=True) if answer_elem else ""
        )

        questions.append(
            PAAQuestion(
                question=question_text, rank=rank, answer_preview=answer_preview
            )
        )

    if not questions:
        return ParseResult(
            status=SurfaceStatus.EMPTY,
            items=[],
            failure_category=None,
        )

    return ParseResult(
        status=SurfaceStatus.OK,
        items=questions,
        failure_category=None,
    )


def _parse_related(
    soup: BeautifulSoup, config: ParserConfig
) -> ParseResult:
    """Extract Related Searches from SERP."""
    related_items = soup.select('div[data-async-context] a[href*="/search?q="]')

    if not related_items:
        related_items = soup.select("g-scrolling-carousel a")

    if not related_items:
        related_items = soup.select("div.EIaa9b a")

    if not related_items:
        return ParseResult(
            status=SurfaceStatus.FAILED,
            items=[],
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
        )

    searches = []
    seen_queries = set()
    for item in related_items:
        query_text = item.get_text(strip=True)
        if not query_text or query_text in seen_queries:
            continue

        seen_queries.add(query_text)
        searches.append(RelatedSearch(query=query_text, rank=len(searches) + 1))

        if len(searches) >= 20:
            break

    if not searches:
        return ParseResult(
            status=SurfaceStatus.EMPTY,
            items=[],
            failure_category=None,
        )

    return ParseResult(
        status=SurfaceStatus.OK,
        items=searches,
        failure_category=None,
    )
