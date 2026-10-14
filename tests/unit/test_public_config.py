from vocalis import public_config


def test_public_assistant_reads_vocalis_settings(monkeypatch):
    for key in (
        'VOCALIS_PUBLIC_ASSISTANT', 'VOCALIS_PUBLIC_TENANT_ID', 'VOCALIS_PUBLIC_TENANT_NAME',
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv('VOCALIS_PUBLIC_ASSISTANT', '1')
    monkeypatch.setenv('VOCALIS_PUBLIC_TENANT_ID', 'vocalis-tenant')

    assert public_config.enabled()
    assert public_config.tenant_id() == 'vocalis-tenant'
    assert public_config.tenant_name() == 'Vocalis'
    assert public_config.is_tenant('vocalis-tenant')


def test_public_assistant_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv('VOCALIS_PUBLIC_ASSISTANT', raising=False)

    assert not public_config.enabled()


def test_public_assistant_can_be_disabled(monkeypatch):
    monkeypatch.setenv('VOCALIS_PUBLIC_ASSISTANT', '0')

    assert not public_config.enabled()
