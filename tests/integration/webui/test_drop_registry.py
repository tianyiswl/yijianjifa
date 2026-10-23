import pytest
from yijianjifa.webui import media_registry


def registry():
    cls=getattr(media_registry,'DropRegistry',None)
    assert cls is not None,'native drop grants are missing'
    return cls()


def test_drop_grant_is_opaque_single_use_and_revoked_on_close(tmp_path):
    r=registry();file=tmp_path/'中文 file.mp4';file.write_bytes(b'fixture')
    token=r.grant([file])
    assert len(token)==32 and str(tmp_path) not in token
    assert r.consume(token)==(file,)
    with pytest.raises(ValueError):r.consume(token)
    token=r.grant([file]);r.close()
    with pytest.raises(ValueError):r.consume(token)
    with pytest.raises(ValueError):r.grant([file])


def test_drop_rejects_symlink_missing_and_empty(tmp_path):
    r=registry();file=tmp_path/'v.mp4';file.write_bytes(b'fixture');link=tmp_path/'link.mp4';link.symlink_to(file)
    for paths in ([],[link],[tmp_path/'missing']):
        with pytest.raises(ValueError):r.grant(paths)
    token=r.grant([tmp_path]);assert r.consume(token)==(tmp_path,)


def test_drop_capacity_evicts_oldest_grant(tmp_path):
    r=registry();file=tmp_path/'v.mp4';file.write_bytes(b'fixture')
    tokens=[r.grant([file]) for _ in range(9)]
    with pytest.raises(ValueError):r.consume(tokens[0])
    assert r.consume(tokens[-1])==(file,)
