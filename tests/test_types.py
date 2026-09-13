import pytest
from hypothesis import given
from hypothesis import strategies as st

from arb.types import (
    TICKS_PER_DOLLAR,
    complement,
    dollars_from_ticks,
    is_valid_price,
    ticks_from_dollars,
)


class TestTicksFromDollars:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0.4200", 4200),
            ("0.555", 5550),
            ("0.01", 100),
            ("1", 10000),
            ("1.0000", 10000),
            (".5", 5000),
            ("1.", 10000),
            ("0.0001", 1),
            ("13.00", 130000),
            ("0", 0),
        ],
    )
    def test_parses_exactly(self, text: str, expected: int) -> None:
        assert ticks_from_dollars(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            ".",
            "0.42001",  # finer than a tick
            "-0.5",
            "+0.5",
            "1e-3",
            "0x10",
            " 0.5",
            "0.5 ",
            "0..5",
            "0.5.0",
            "\u0660.\u0665",  # Arabic-Indic 0.5: int() accepts these digits, we must not
            "nan",
            "inf",
        ],
    )
    def test_rejects_garbage(self, text: str) -> None:
        with pytest.raises(ValueError):
            ticks_from_dollars(text)

    @given(st.integers(min_value=0, max_value=3 * TICKS_PER_DOLLAR))
    def test_round_trips_with_dollars_from_ticks(self, ticks: int) -> None:
        assert ticks_from_dollars(dollars_from_ticks(ticks)) == ticks


class TestDollarsFromTicks:
    def test_formats_four_decimals(self) -> None:
        assert dollars_from_ticks(4200) == "0.4200"
        assert dollars_from_ticks(1) == "0.0001"
        assert dollars_from_ticks(10000) == "1.0000"

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            dollars_from_ticks(-1)


class TestPriceRules:
    def test_valid_price_is_exclusive_of_bounds(self) -> None:
        assert not is_valid_price(0)
        assert is_valid_price(1)
        assert is_valid_price(9999)
        assert not is_valid_price(10000)
        assert not is_valid_price(-1)
        assert not is_valid_price(10001)

    @given(st.integers(min_value=1, max_value=TICKS_PER_DOLLAR - 1))
    def test_complement_is_an_involution_and_stays_valid(self, price: int) -> None:
        assert complement(complement(price)) == price
        assert is_valid_price(complement(price))
