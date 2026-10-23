from tests.integration.downloads.test_runtime import runtime


def test_model_location_displays_backend_cache_without_verifying_models(runtime, tmp_path):
    worker, _, _, models, _ = runtime
    models.root = tmp_path / '本地模型缓存'
    calls = models.calls
    result = worker.query('download.models.list', {})
    assert result['cache_path'] == str(models.root)
    assert models.calls == calls
