import pytest
from vocalis.evaluate import run


@pytest.mark.asyncio
async def test_seeded_scenarios_pass():
    report=await run()
    assert report['mode']=='deterministic_fixture'
    assert report['passed']==report['total']==7
