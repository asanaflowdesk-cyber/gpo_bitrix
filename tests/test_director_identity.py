from eqazyna_bitrix.director import director_identity_key, director_keys_match, split_director_fio


def test_abbreviated_and_full_reversed_director_names_match():
    assert director_keys_match("ЛЯБАХ Г.Г.", "Геннадий Лябах")


def test_director_identity_key_prefers_surname_initial_alias():
    assert director_identity_key("ЛЯБАХ Г.Г.").startswith("surname_initial")
    assert director_identity_key("Геннадий Лябах").startswith("surname_")


def test_split_director_fio_reverses_common_first_name_surname_form():
    fio = split_director_fio("Геннадий Лябах")

    assert fio is not None
    assert fio.last_name == "ЛЯБАХ"
    assert fio.name == "ГЕННАДИЙ"
