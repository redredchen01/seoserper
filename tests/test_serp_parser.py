"""Tests for SERP parser (PAA and Related Searches extraction)."""

import json
from pathlib import Path

import pytest

from seoserper.models import (
    FailureCategory,
    ParseResult,
    RelatedSearch,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.parsers.serp import ParserConfig, parse_serp


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "serp"


class TestParseEmptyHtml:
    def test_empty_string_returns_both_failed(self):
        result = parse_serp("")
        assert result[SurfaceName.PAA].status == SurfaceStatus.FAILED
        assert result[SurfaceName.RELATED].status == SurfaceStatus.FAILED

    def test_en_us_fixture_parses_correctly(self):
        html = (FIXTURES_DIR / "en-us.html").read_text()
        result = parse_serp(html, locale="en-us")
        assert result[SurfaceName.PAA].status == SurfaceStatus.OK
        assert len(result[SurfaceName.PAA].items) == 4
        assert result[SurfaceName.RELATED].status == SurfaceStatus.OK


class TestParseZhCn:
    def test_zh_cn_fixture_parses_correctly(self):
        html = (FIXTURES_DIR / "zh-cn.html").read_text()
        result = parse_serp(html, locale="zh-cn")
        assert result[SurfaceName.PAA].status == SurfaceStatus.OK
        assert len(result[SurfaceName.PAA].items) == 5


class TestParseJaJp:
    def test_ja_jp_fixture_parses_correctly(self):
        html = (FIXTURES_DIR / "ja-jp.html").read_text()
        result = parse_serp(html, locale="ja-jp")
        assert result[SurfaceName.PAA].status == SurfaceStatus.OK
        assert len(result[SurfaceName.PAA].items) == 3


class TestEmptyPaa:
    def test_empty_paa_fixture_has_no_questions(self):
        html = (FIXTURES_DIR / "empty-paa.html").read_text()
        result = parse_serp(html)
        assert result[SurfaceName.PAA].status == SurfaceStatus.FAILED
        assert result[SurfaceName.RELATED].status == SurfaceStatus.OK


class TestSelectorBroken:
    def test_selector_broken_fixture_fails_both(self):
        html = (FIXTURES_DIR / "selector-broken.html").read_text()
        result = parse_serp(html)
        assert result[SurfaceName.PAA].status == SurfaceStatus.FAILED
        assert result[SurfaceName.RELATED].status == SurfaceStatus.FAILED
