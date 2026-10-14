from vocalis import public_config


def test_public_assistant_reads_legacy_render_settings(monkeypatch):
    for key in (
        'VOCALIS_PUBLIC_ASSISTANT', 'VOCALIS_PUBLIC_TENANT_ID',
        'VOCALIS_DEMO_TENANT_ID', 'VOCALIS_DEMO_TENANT_NAME',
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv('VOCALIS_PUBLIC_DEMO', '1')
    monkeypatch.setenv('VOCALIS_DEMO_TENANT_ID', 'existing-tenant')

    assert public_config.enabled()
    assert public_config.tenant_id() == 'existing-tenant'
    assert public_config.tenant_name() == 'Vocalis'
    assert public_config.is_tenant('existing-tenant')


def test_current_public_setting_can_disable_legacy_render_flag(monkeypatch):
    monkeypatch.setenv('VOCALIS_PUBLIC_DEMO', '1')
    monkeypatch.setenv('VOCALIS_PUBLIC_ASSISTANT', '0')

    assert not public_config.enabled()
