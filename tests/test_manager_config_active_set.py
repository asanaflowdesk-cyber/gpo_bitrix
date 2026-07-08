from eqazyna_bitrix.config.managers import load_manager_config


def test_commented_managers_do_not_participate_in_distribution():
    config = load_manager_config()

    # Активные сейчас: Талдыкорган 74/76/78 + УК 70 + Павлодар 110.
    assert set(config.allowed_user_ids) == {74, 76, 78, 70, 110}

    commented_ids = {100, 98, 72, 102, 80, 84, 86, 88, 106, 116, 92, 108, 104, 94, 114, 90}
    assert commented_ids.isdisjoint(config.allowed_user_ids)
