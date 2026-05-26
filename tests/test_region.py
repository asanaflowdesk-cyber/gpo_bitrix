from eqazyna_bitrix.region import detect_region


def test_detect_region_almaty():
    assert detect_region("Казахстан, г. Алматы, ул. Абая") == "г. Алматы"


def test_detect_region_karaganda():
    assert detect_region("Карагандинская область, г. Караганда") == "Карагандинская область"
