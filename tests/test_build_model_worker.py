import pytest

from scripts.build_model_worker import build


def test_existing_staging_directory_is_never_replaced(tmp_path):
    destination = tmp_path / 'existing'
    destination.mkdir()
    marker = destination / 'marker'
    marker.write_text('keep')
    with pytest.raises(FileExistsError):
        build(destination)
    assert marker.read_text() == 'keep'
