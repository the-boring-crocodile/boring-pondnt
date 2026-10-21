import pytest

from boring_pondnt import d365


@pytest.mark.parametrize("value", ["Contoso", "my.model-1", "a_b"])
def test_plain_names_pass(value):
    assert d365.checked_name("model", value) == value


@pytest.mark.parametrize("value", ["", ".", "..", "...", "a\n", "a/b", "a b", "x" * 129])
def test_other_names_refused(value):
    with pytest.raises(d365.Refused):
        d365.checked_name("model", value)
