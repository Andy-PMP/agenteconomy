"""A small economy for tool-layer tests, funding the two accounts they use."""

from agenteconomy.chain.vectors import sample_keys
from agenteconomy.economy.world import economy_params


def params():
    allocations = [(sample_keys(name).public_hex, 1000) for name in ("collector", "scammer")]
    return economy_params(allocations, chain_id="tools-test", difficulty_bits=4)
