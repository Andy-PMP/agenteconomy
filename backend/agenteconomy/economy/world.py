"""Runs the economy one day at a time: fresh nodes from the saved chain, agent turns, settle, report, save.

Keys are never saved. On a restart they're supplied again; P6 gets them from a secrets manager.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..chain.block import Block
from ..chain.keys import KeyPair
from ..chain.params import ChainParams, Season
from ..chain.state import State, apply_block
from ..chain.transaction import sign_transfer
from ..chain.validation import make_genesis, validate_chain
from ..network.settle import Cluster, SettleResult
from .agents import ScriptedAgent
from .market import Trade, average_prices, net_worth, trades_in_block
from .messages import MessageBoard
from .shocks import Shock, conditions_for, roll_shocks
from .tools import AgentTools, DayFacts

DAY_MS = 86_400_000
RECENT_TRADES = 50
MAX_CLOSING_ATTEMPTS = 200
FIRST_SEASON = ("blueA", "greenB", "redC", "yellowD", "whiteE", "darkF", "blueB", "redE")


def _dropped_by_sender(result: SettleResult) -> dict[str, int]:
    dropped: dict[str, int] = {}
    for tx in result.pending:
        dropped[tx.sender] = dropped.get(tx.sender, 0) + 1
    return dict(sorted(dropped.items()))


def _describe_trade(trade: Trade, address: str, names: dict[str, str]) -> dict[str, Any]:
    """One side of a trade, for an agent's memory."""
    proposing = trade.proposer == address
    other = trade.acceptor if proposing else trade.proposer
    gave_coins, got_coins = (trade.give_coins, trade.want_coins) if proposing else (trade.want_coins, trade.give_coins)
    gave, got = trade.give_creatures, trade.want_creatures
    if not proposing:
        gave, got = got, gave
    return {"with": names.get(other, other[:8]), "gave_coins": gave_coins, "got_coins": got_coins,
            "gave_creatures": len(gave), "got_creatures": len(got)}


def economy_params(allocations: Sequence[tuple[str, int]], *, chain_id: str = "agenteconomy-1",
                   difficulty_bits: int = 16, **overrides: Any) -> ChainParams:
    settings = {"chain_id": chain_id, "difficulty_bits": difficulty_bits, "block_reward": 50, "min_fee": 1,
                "supply_cap": 100_000, "trade_fee_bps": 200, "seasons": (Season(1, FIRST_SEASON),),
                "genesis_allocations": tuple(allocations), **overrides}
    return ChainParams(**settings)


@dataclass(frozen=True)
class WorldConfig:
    params: ChainParams
    seed: str
    hash_budget: int = 1 << 16
    turns_per_day: int = 8
    turn_ms: int = 2_000
    turn_jitter_ms: int = 1_500  # overlapping turns are what let two agents mine at nearly the same moment
    fee: int = 1
    action_limit: int = 12
    mine_limit: int | None = None  # mine actions per agent per day
    closing_blocks: bool = False  # mine what's still waiting at the end of the day
    message_limit: int = 3  # messages per agent per day
    max_spend_bps: int | None = None  # the most of its coins an agent can pay in one trade
    offer_ttl: int = 50
    min_latency_ms: int = 20
    max_latency_ms: int = 800


@dataclass(frozen=True)
class Member:
    agent: ScriptedAgent
    keys: KeyPair


@dataclass
class DayReport:
    day: int
    tip: str
    height: int
    new_blocks: int
    orphaned: int
    reorgs: int
    settle_rounds: int
    actions: int
    invalid_actions: int
    invalid_by_code: dict[str, int]
    trades: int
    dropped_pending: list[str]
    shocks: list[dict]
    supply: int
    burned: int
    creatures: int
    open_offers: int
    log_digest: str
    dropped_by_sender: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    model_cost_nanos: int = 0
    model_errors: int = 0
    refused_calls: int = 0
    security: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class _Grant:
    keys: KeyPair
    amount: int


@dataclass
class World:
    config: WorldConfig
    members: list[Member]
    treasury: KeyPair | None = None
    blocks: list[Block] = field(default_factory=list)
    state: State = field(init=False)
    reports: list[DayReport] = field(default_factory=list)
    shocks: list[Shock] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    last_log: list[dict] = field(default_factory=list)
    ai_run: Any = None  # model accounting, when the agents use a model
    last_day_record: dict | None = None
    board: MessageBoard = field(default_factory=MessageBoard)
    security: Any = None  # sends and judges the day's attacks, when there are any
    _grants: list[_Grant] = field(default_factory=list)

    def __post_init__(self):
        if not self.blocks:
            self.blocks = [make_genesis(self.config.params)]
        self.state = validate_chain(self.blocks, self.config.params)
        self.trades = self._trades_from_blocks()

    def _trades_from_blocks(self) -> list[Trade]:
        """Trades aren't saved. They come from the chain, so a restart shows agents exactly what they'd have seen."""
        trades: list[Trade] = []
        state = apply_block(State(), self.blocks[0], self.config.params)
        for block in self.blocks[1:]:
            trades += trades_in_block(block, state)
            state = apply_block(state, block, self.config.params)
        return trades

    @property
    def day(self) -> int:
        return len(self.reports)

    def add_member(self, agent: ScriptedAgent, keys: KeyPair, grant: int) -> None:
        """A new agent joins next day, funded by an ordinary signed transfer anyone can see. Needs the treasury."""
        if self.treasury is None:
            raise ValueError("funding a new agent needs the treasury key")
        self.members.append(Member(agent, keys))
        self._grants.append(_Grant(keys, grant))

    def run_day(self) -> DayReport:
        config, day = self.config, self.day
        previous_tip = self.blocks[-1].header.hash_hex
        pool = config.params.species_pool(self.blocks[-1].header.height + 1) or ()
        todays = roll_shocks(config.seed, day, previous_tip, pool) if pool else []
        self.shocks += todays
        conditions = conditions_for(day, self.shocks)

        start_time = max(config.params.genesis_timestamp + day * DAY_MS, self.blocks[-1].header.timestamp)
        cluster = Cluster(config.params, f"{config.seed}:{day}", chain=self.blocks, trusted_state=self.state,
                          start_time=start_time, min_latency_ms=config.min_latency_ms,
                          max_latency_ms=config.max_latency_ms)
        if self.ai_run is not None:
            self.ai_run.begin_day(day)
        for member in self.members:
            member.agent.begin_day(day)
        if self.security is not None:
            self.security.send_attacks(self, cluster, day)
        self._pay_grants(cluster)
        self._schedule_turns(cluster, day, conditions, tuple(shock.kind for shock in todays))
        cluster.run_for(config.turns_per_day * config.turn_ms + config.turn_jitter_ms)
        if config.closing_blocks:
            self._mine_closing_blocks(cluster)

        state_before, height_before = self.state, self.blocks[-1].header.height
        result = cluster.settle()
        self.blocks = result.blocks
        self.state = result.state
        self.last_log = cluster.log.entries
        self._end_day(cluster, result, day, height_before, state_before, conditions, todays)
        security = {} if self.security is None else self.security.judge_day(self, cluster, day, height_before,
                                                                            state_before)
        report = self._report(cluster, result, day, height_before, state_before, todays)
        report.security = security
        if self.ai_run is not None:
            self.last_day_record = self.ai_run.day_record()
        self.reports.append(report)
        return report

    def _end_day(self, cluster: Cluster, result: SettleResult, day: int, height_before: int, state_before: State,
                 conditions, todays: list[Shock]) -> None:
        """Agents hear what really happened: from the settled chain and the log, not from what they believed."""
        names = {member.keys.public_hex: member.agent.shown_as for member in self.members}
        new_blocks = result.blocks[height_before + 1:]
        trades, state = [], state_before
        for block in new_blocks:
            trades += trades_in_block(block, state)
            state = apply_block(state, block, self.config.params)
        dropped = _dropped_by_sender(result)
        for member in self.members:
            address = member.keys.public_hex
            failed: dict[str, int] = {}
            for entry in cluster.log.of_kind("agent_action"):
                if entry["node"] == member.agent.name and not entry["ok"]:
                    failed[entry["code"]] = failed.get(entry["code"], 0) + 1
            member.agent.end_day(DayFacts(
                day=day,
                coins_start=state_before.balance(address),
                coins_end=result.state.balance(address),
                worth_start=net_worth(state_before, address, conditions),
                worth_end=net_worth(result.state, address, conditions),
                blocks_mined=sum(block.txs[0].body["to"] == address for block in new_blocks),
                trades=tuple(_describe_trade(trade, address, names) for trade in trades
                             if address in (trade.proposer, trade.acceptor)),
                failed_actions=dict(sorted(failed.items())),
                dropped_transactions=dropped.get(address, 0),
                shocks=tuple(shock.kind for shock in todays),
            ))
            self._log_decisions(member.agent, cluster.log)

    def _mine_closing_blocks(self, cluster: Cluster) -> None:
        """Agents with a daily mining limit mine early and act later, so without this nothing they do late gets mined.

        It spends equal hash budgets in turn, with no model involved, and only while something can be mined.
        """
        rng = cluster.sim.rng("closing")
        miners = list(enumerate(self.members))
        for _ in range(MAX_CLOSING_ATTEMPTS):
            cluster.sim.run_until_idle()
            if not any(node.mempool.select(node.state, 1) for node in cluster.nodes):
                return
            index, member = rng.choice(miners)
            node = cluster.nodes[index % len(cluster.nodes)]
            block = node.mine(member.keys.public_hex, self.config.hash_budget)
            if block is not None:
                cluster.log.record(member.agent.name, "closing_block", block=block.header.hash_hex)

    def _pay_grants(self, cluster: Cluster) -> None:
        node = cluster.nodes[0]
        for offset, grant in enumerate(self._grants):
            nonce = node.state.nonce(self.treasury.public_hex) + offset
            node.submit_tx(sign_transfer(self.treasury, self.config.params.chain_id, to=grant.keys.public_hex,
                                         amount=grant.amount, nonce=nonce, fee=self.config.fee))
        self._grants = []

    def _schedule_turns(self, cluster: Cluster, day: int, conditions, shocks: tuple[str, ...]) -> None:
        config = self.config
        addresses = [member.keys.public_hex for member in self.members]
        dropped = self.reports[-1].dropped_by_sender if self.reports else {}
        names = {member.keys.public_hex: member.agent.name for member in self.members}
        shown_names = {member.keys.public_hex: member.agent.shown_as for member in self.members}
        prices = average_prices(self.trades[-RECENT_TRADES:], self.state)
        schedule = cluster.sim.rng("schedule")
        for index, member in enumerate(self.members):
            rivals = tuple(address for address in addresses if address != member.keys.public_hex)
            tools = AgentTools(member.agent.name, member.keys, cluster.nodes[index % len(cluster.nodes)], cluster.log,
                               hash_budget=config.hash_budget, fee=config.fee, action_limit=config.action_limit,
                               offer_ttl=config.offer_ttl, day=day, conditions=conditions, rivals=rivals, shocks=shocks,
                               dropped_yesterday=dropped.get(member.keys.public_hex, 0), names=names,
                               prices=prices, recent_trades=tuple(self.trades[-RECENT_TRADES:]),
                               mine_limit=config.mine_limit, board=self.board,
                               message_limit=config.message_limit, max_spend_bps=config.max_spend_bps,
                               shown_names=shown_names)
            rng = cluster.sim.rng(f"agent:{member.agent.name}")

            def turn(tools=tools, agent=member.agent, rng=rng, log=cluster.log) -> None:
                action = agent.decide(tools.view(), rng)
                self._log_decisions(agent, log)
                if action is not None:
                    agent.after_action(action, tools.execute(action))

            for number in range(config.turns_per_day):
                cluster.sim.schedule(number * config.turn_ms + schedule.randint(0, config.turn_jitter_ms), turn)

    def _log_decisions(self, agent: ScriptedAgent, log) -> None:
        for decision in agent.drain_decisions():
            log.record(agent.name, "agent_decision", **decision)
            self.board.mark_read(decision.get("messages_read", []))

    def _report(self, cluster: Cluster, result: SettleResult, day: int, height_before: int, state_before: State,
                todays: list[Shock]) -> DayReport:
        actions = cluster.log.of_kind("agent_action")
        invalid: dict[str, int] = {}
        for action in actions:
            if not action["ok"]:
                invalid[action["code"]] = invalid.get(action["code"], 0) + 1

        new_trades: list[Trade] = []
        state = state_before
        for block in result.blocks[height_before + 1:]:
            new_trades += trades_in_block(block, state)
            state = apply_block(state, block, self.config.params)
        self.trades += new_trades

        return DayReport(
            day=day, tip=result.tip, height=result.height, new_blocks=result.height - height_before,
            orphaned=len(result.orphaned), reorgs=result.reorgs, settle_rounds=result.rounds, actions=len(actions),
            invalid_actions=sum(invalid.values()), invalid_by_code=dict(sorted(invalid.items())),
            trades=len(new_trades), dropped_pending=[tx.id.hex() for tx in result.pending],
            shocks=[shock.to_dict() for shock in todays], supply=result.state.supply, burned=result.state.burned,
            creatures=len(result.state.creatures), open_offers=len(result.state.offers),
            log_digest=cluster.log.digest(), dropped_by_sender=_dropped_by_sender(result),
            **(self.ai_run.totals() if self.ai_run is not None else {}),
        )

    def save(self, path: Path) -> None:
        """Chain and history only, never keys."""
        memories = {member.agent.name: member.agent.memory_dict() for member in self.members
                    if member.agent.memory_dict() is not None}
        data = {"blocks": [block.to_dict() for block in self.blocks], "reports": [r.to_dict() for r in self.reports],
                "shocks": [shock.to_dict() for shock in self.shocks], "memories": memories,
                "messages": self.board.to_dict()}
        path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, config: WorldConfig, members: list[Member], treasury: KeyPair | None = None,
             ai_run: Any = None, security: Any = None) -> World:
        """Restart from saved data. Creating a World replays the whole chain, so a damaged file can't go unnoticed."""
        data = json.loads(path.read_text(encoding="utf-8"))
        blocks = [Block.from_dict(block) for block in data["blocks"]]
        for member in members:
            if member.agent.name in data["memories"]:
                member.agent.load_memory(data["memories"][member.agent.name])
        return cls(config, members, treasury, blocks=blocks, ai_run=ai_run, security=security,
                   board=MessageBoard.from_dict(data["messages"]),
                   reports=[DayReport(**report) for report in data["reports"]],
                   shocks=[Shock(**shock) for shock in data["shocks"]])

