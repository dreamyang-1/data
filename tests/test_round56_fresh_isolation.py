"""Fresh mode must fail before provider setup when given prior state receipts."""
from types import SimpleNamespace

import pytest

from tools.cutover import round55_live_chains as live


@pytest.mark.asyncio
@pytest.mark.parametrize('seed,prior', [('previous.json', []), (None, ['previous-run'])])
async def test_fresh_mode_rejects_any_prefix_restore_before_model_setup(monkeypatch, seed, prior):
    def forbidden():
        pytest.fail('fresh isolation must be checked before provider configuration')
    monkeypatch.setattr(live.native, 'Settings', forbidden)
    with pytest.raises(AssertionError, match='FRESH_CHAIN_MUST_NOT_RESTORE_PRIOR_RECEIPTS'):
        await live.run(SimpleNamespace(independent_sessions=True, seed=seed, prior_output=prior))
