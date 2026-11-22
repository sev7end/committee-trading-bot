from tradingbot.execution import limit_price_for, choose_base


def test_sane_quote_uses_ask():
    assert choose_base(100.0, 100.1, 99.5, 98.0) == (100.1, "ask")


def test_stale_wide_quote_falls_back_to_last_trade():
    # 10% spread: pre-market garbage from the free feed -> use the latest trade instead
    assert choose_base(318.0, 351.7, 333.2, 333.0) == (333.2, "last_trade")


def test_no_quote_no_trade_uses_reference():
    base, src = choose_base(None, None, None, 100.0)
    assert src == "ref_close" and abs(base - 100.1) < 1e-9


def test_limit_adds_buffer():
    assert limit_price_for(100.0, 99.0, 20, bid=99.95, last=99.98) == 100.2
    assert limit_price_for(351.7, 333.0, 20, bid=318.0, last=333.2) == round(333.2 * 1.002, 2)
