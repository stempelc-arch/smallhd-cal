from smallhd_cal.displays import Display, choose_external_display


def test_choose_external_display_prefers_non_main_display() -> None:
    main = Display(display_id=1, x=0, y=0, width=1440, height=900, is_main=True)
    external = Display(display_id=2, x=1440, y=0, width=1920, height=1080, is_main=False)

    assert choose_external_display([main, external]) == external


def test_choose_external_display_falls_back_to_main_display() -> None:
    main = Display(display_id=1, x=0, y=0, width=1440, height=900, is_main=True)

    assert choose_external_display([main]) == main


def test_choose_external_display_returns_none_for_empty_list() -> None:
    assert choose_external_display([]) is None


def test_display_geometry() -> None:
    display = Display(display_id=2, x=-1920, y=0, width=1920, height=1080)

    assert display.geometry == "1920x1080-1920+0"
