import app


def test_audio_ingress_checks_sample_rate_and_duration_before_decode():
    assert app.audio_payload_error({'audio':'AAAA','sample_rate':48000}) is None
    assert app.audio_payload_error({'audio':'A'*3_840_004,'sample_rate':48000}) == 'audio_exceeds_30_seconds'
    assert app.audio_payload_error({'audio':'A'*1_280_004,'sample_rate':16000}) == 'audio_exceeds_30_seconds'
    assert app.audio_payload_error({'audio':'AAAA','sample_rate':True}) == 'invalid_sample_rate'


def test_audio_ingress_rejects_empty_wrong_type_and_oversized_payloads():
    assert app.audio_payload_error({'audio':''}) == 'audio_required'
    assert app.audio_payload_error({'audio':b'AAAA'}) == 'audio_required'
    assert app.audio_payload_error({'audio':'A'*4_000_001}) == 'audio_too_large'
