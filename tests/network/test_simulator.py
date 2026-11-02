import pytest

from agenteconomy.network.network import EventLog, Network
from agenteconomy.network.simulator import Simulator


def test_events_run_in_time_order():
    sim = Simulator(1)
    seen = []
    sim.schedule(30, lambda: seen.append("late"))
    sim.schedule(10, lambda: seen.append("early"))
    sim.run_until_idle()
    assert seen == ["early", "late"]
    assert sim.now == 30


def test_events_at_the_same_time_run_in_the_order_scheduled():
    sim = Simulator(1)
    seen = []
    for i in range(5):
        sim.schedule(10, lambda i=i: seen.append(i))
    sim.run_until_idle()
    assert seen == [0, 1, 2, 3, 4]


def test_cancelled_event_never_runs_and_does_not_move_the_clock():
    sim = Simulator(1)
    seen = []
    event = sim.schedule(10, lambda: seen.append("x"))
    event.cancel()
    sim.run_until_idle()
    assert seen == []
    assert sim.now == 0


def test_run_until_stops_at_the_time_and_keeps_later_events():
    sim = Simulator(1)
    seen = []
    sim.schedule(10, lambda: seen.append(10))
    sim.schedule(20, lambda: seen.append(20))
    sim.run_until(10)
    assert seen == [10] and sim.now == 10
    sim.run_until(15)
    assert seen == [10] and sim.now == 15
    sim.run_until_idle()
    assert seen == [10, 20]


def test_run_until_never_moves_the_clock_backwards():
    sim = Simulator(1, start_time=100)
    sim.run_until(50)
    assert sim.now == 100


def test_events_can_schedule_more_events():
    sim = Simulator(1)
    seen = []
    sim.schedule(5, lambda: sim.schedule(5, lambda: seen.append(sim.now)))
    sim.run_until_idle()
    assert seen == [10]


@pytest.mark.parametrize("delay", [-1, 1.5, True, "1"])
def test_bad_delays_are_refused(delay):
    with pytest.raises(ValueError):
        Simulator(1).schedule(delay, lambda: None)


def test_endless_loop_raises_instead_of_hanging():
    sim = Simulator(1)

    def again():
        sim.schedule(1, again)

    sim.schedule(1, again)
    with pytest.raises(RuntimeError):
        sim.run_until_idle(max_events=100)


def test_random_streams_are_seeded_and_independent():
    first, second = Simulator(1), Simulator(1)
    values = [first.rng("a").random() for _ in range(3)]
    second.rng("b").random()  # using another stream must not disturb this one
    assert [second.rng("a").random() for _ in range(3)] == values
    assert Simulator(2).rng("a").random() != values[0]
    assert first.rng("a") is first.rng("a")


def make_network(**options):
    sim = Simulator(1)
    network = Network(sim, **options)
    inbox = {name: [] for name in "abc"}
    reconnects = []
    for name in "abc":
        network.register(name, lambda sender, data, name=name: inbox[name].append((sender, data, sim.now)),
                         lambda name=name: reconnects.append(name))
    return sim, network, inbox, reconnects


def test_messages_arrive_within_the_latency_range():
    sim, network, inbox, _ = make_network(min_latency_ms=50, max_latency_ms=60)
    for _ in range(100):
        network.send("a", "b", b"x")
    sim.run_until_idle()
    times = {time for _, _, time in inbox["b"]}
    assert len(inbox["b"]) == 100
    assert min(times) >= 50 and max(times) <= 60
    assert len(times) > 1


def test_latency_can_reorder_messages():
    sim, network, inbox, _ = make_network(min_latency_ms=1, max_latency_ms=1000)
    for i in range(50):
        network.send("a", "b", bytes([i]))
    sim.run_until_idle()
    received = [data for _, data, _ in inbox["b"]]
    assert received != sorted(received)


def test_peers_exclude_self():
    _, network, _, _ = make_network()
    assert network.peers_of("a") == ["b", "c"]


def test_unknown_destination_is_dropped():
    sim, network, _, _ = make_network()
    network.send("a", "nobody", b"x")
    sim.run_until_idle()
    assert network.dropped == 1


@pytest.mark.parametrize("options", [{"min_latency_ms": -1}, {"min_latency_ms": 10, "max_latency_ms": 5},
                                     {"drop_rate": 1.5}, {"drop_rate": -0.1}])
def test_bad_network_settings_are_refused(options):
    with pytest.raises(ValueError):
        Network(Simulator(1), **options)


def test_drop_rate_is_roughly_honored():
    sim, network, inbox, _ = make_network(drop_rate=0.3)
    for _ in range(1000):
        network.send("a", "b", b"x")
    sim.run_until_idle()
    assert 620 < len(inbox["b"]) < 780
    assert network.dropped == 1000 - len(inbox["b"])


def test_drop_rate_of_one_delivers_nothing():
    sim, network, inbox, _ = make_network(drop_rate=1.0)
    network.send("a", "b", b"x")
    sim.run_until_idle()
    assert inbox["b"] == []


def test_partition_blocks_across_groups_only():
    sim, network, inbox, _ = make_network()
    network.partition(["a"], ["b", "c"])
    network.send("a", "b", b"x")
    network.send("b", "c", b"y")
    sim.run_until_idle()
    assert inbox["b"] == []
    assert len(inbox["c"]) == 1


def test_unlisted_endpoints_share_a_group():
    sim, network, inbox, _ = make_network()
    network.partition(["a"])
    network.send("b", "c", b"x")
    network.send("a", "c", b"y")
    sim.run_until_idle()
    assert [data for _, data, _ in inbox["c"]] == [b"x"]


def test_message_in_flight_is_lost_if_the_link_goes_down():
    sim, network, inbox, _ = make_network()
    network.send("a", "b", b"x")
    network.partition(["a"], ["b"])
    sim.run_until_idle()
    assert inbox["b"] == []
    assert network.dropped == 1


def test_offline_endpoint_sends_and_receives_nothing_until_back():
    sim, network, inbox, reconnects = make_network()
    network.set_offline("b")
    network.send("a", "b", b"x")
    network.send("b", "a", b"y")
    sim.run_until_idle()
    assert inbox["a"] == inbox["b"] == []

    network.set_offline("b", False)
    assert reconnects == ["b"]
    network.send("a", "b", b"x")
    sim.run_until_idle()
    assert len(inbox["b"]) == 1


def test_coming_back_online_only_announces_if_it_was_offline():
    _, network, _, reconnects = make_network()
    network.set_offline("b", False)
    assert reconnects == []


def test_heal_restores_everything_and_tells_every_endpoint():
    sim, network, inbox, reconnects = make_network()
    network.partition(["a"], ["b"])
    network.set_offline("c")
    network.heal()
    assert reconnects == ["a", "b", "c"]
    network.send("a", "b", b"x")
    network.send("a", "c", b"x")
    sim.run_until_idle()
    assert len(inbox["b"]) == len(inbox["c"]) == 1


def test_log_records_time_and_digest_depends_on_contents():
    sim = Simulator(1, start_time=7)
    first, second = EventLog(sim), EventLog(sim)
    first.record("n", "kind_a", value=1)
    second.record("n", "kind_a", value=1)
    assert first.entries == [{"time": 7, "node": "n", "kind": "kind_a", "value": 1}]
    assert first.digest() == second.digest()
    second.record("n", "kind_b")
    assert first.digest() != second.digest()
    assert len(second.of_kind("kind_b")) == 1
